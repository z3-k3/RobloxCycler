"""Command line entry for listing outfits and cycling them."""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from robloxcycler.api import RobloxClient, RobloxError
from robloxcycler.logic import (
    MIN_INTERVAL_SECONDS,
    active_interval,
    api_block_reason,
    is_avatar_outfit,
    cookie_from_text,
    load_dotenv,
    load_json,
    merge_outfits,
    normalize_cookie,
    read_config,
    save_json,
    select_outfits,
    slowed_interval,
)

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Equip your saved Roblox outfits, one after another, on your public avatar.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=("help", "whoami", "outfits", "once", "run", "reset"),
        help="whoami, outfits, once, run, or reset",
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args(argv)

    if args.command == "help":
        parser.print_help()
        print()
        print("Start here: copy .env.example to .env, paste your cookie, then run")
        print("  python main.py outfits")
        print("  python main.py once")
        print("  python main.py run")
        return 0

    load_dotenv(ROOT / ".env")
    try:
        config = read_config(_config_path(args.config))
    except Exception as exc:
        print(f"Config problem: {exc}")
        return 1

    if args.command == "reset":
        return reset_slowdown(config)

    cookie = _cookie()
    if not cookie:
        print("No cookie found. Copy .env.example to .env and set ROBLOSECURITY.")
        print("The README explains where to copy it from in your browser.")
        return 1

    client = RobloxClient(cookie, config["request_timeout_seconds"])
    try:
        user = client.whoami()
        print(f"Signed in as {user['name']} ({user['id']})")
        if args.command == "whoami":
            return 0
        outfits = _prepare_outfits(client, int(user["id"]), config)
        if args.command == "outfits":
            _print_outfits(outfits["merged"], outfits["selected"])
            return 0
        selected = outfits["selected"]
        if len(selected) < 2:
            print("You need at least two saved Avatar outfits to cycle.")
            print("On roblox.com, open Avatar, dress the character, and save an outfit. Do that twice.")
            print("Then run: python main.py outfits")
            return 1
        if args.command == "once":
            return wear_once(client, selected)
        return run_loop(client, selected, config)
    except RobloxError as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        client.close()


def reset_slowdown(config: dict[str, Any]) -> int:
    state = _load_state()
    state["slowed"] = False
    state["cooldown_until"] = None
    state["interval_seconds"] = active_interval(
        config["interval_seconds"],
        {"slowed": False},
        config["max_interval_seconds"],
    )
    _save_state(state)
    print(f"Cleared the rate-limit slowdown. Next run uses {state['interval_seconds']:.0f}s from config.json.")
    return 0


def wear_once(client: RobloxClient, selected: list[dict[str, Any]]) -> int:
    state = _load_state()
    index = int(state.get("next_index") or 0) % len(selected)
    outfit = selected[index]
    result = client.wear_outfit(int(outfit["id"]))
    if result.rate_limited:
        print("Roblox rate limited this request. Nothing was changed. Wait, then try again.")
        return 2
    blocked = api_block_reason(result.status, result.message)
    if blocked:
        print(f"Roblox blocked API access ({result.status}): {blocked}")
        print("Stopped. Wait until that block lifts before running this again.")
        return 1
    if result.status == 401:
        print("The cookie expired. Paste a new .ROBLOSECURITY value into .env.")
        return 1
    if not result.ok:
        print(f"Could not equip {outfit['name']}: {result.status} {result.message}")
        return 1
    state["next_index"] = (index + 1) % len(selected)
    state["last_outfit_id"] = outfit["id"]
    state["last_outfit_name"] = outfit["name"]
    state["last_status"] = result.status
    _save_state(state)
    print(f"Equipped \"{outfit['name']}\" ({outfit['id']}).")
    print("Open your Roblox profile and hard-refresh. The 3D avatar updates first; the 2D picture can lag.")
    return 0


def run_loop(client: RobloxClient, selected: list[dict[str, Any]], config: dict[str, Any]) -> int:
    if config["interval_was_raised"]:
        print(
            f"config interval is below {MIN_INTERVAL_SECONDS:.0f}s. "
            f"Using {MIN_INTERVAL_SECONDS:.0f}s, which is as fast as this tool will go."
        )
    state = _load_state()
    interval = active_interval(
        config["interval_seconds"],
        state,
        config["max_interval_seconds"],
    )
    if state.get("slowed"):
        print(
            f"Using {interval:.0f}s between outfits because an earlier run was rate limited. "
            "python main.py reset goes back to config.json."
        )
    print(f"Cycling {len(selected)} outfits every {interval:.0f}s. Ctrl+C stops it.")
    print("A 429 makes the gap longer. A block on the account stops the program.")
    _log(f"start count={len(selected)} interval={interval:.0f}s")

    failures = 0
    while True:
        _wait_out_cooldown(state)
        interval = active_interval(
            config["interval_seconds"],
            state,
            config["max_interval_seconds"],
        )
        index = int(state.get("next_index") or 0) % len(selected)
        outfit = selected[index]
        try:
            result = client.wear_outfit(int(outfit["id"]))
        except RobloxError as exc:
            if exc.transient:
                print(f"{exc} Waiting {interval:.0f}s.")
                _log(f"network-wait {exc}")
                _sleep(interval)
                continue
            print(exc)
            _log(f"auth-stop {exc}")
            return 1

        if result.rate_limited:
            decision = _record_rate_limit(result, state, config, interval, outfit)
            if decision == "stop":
                return 2
            failures = 0
            continue

        blocked = api_block_reason(result.status, result.message)
        if blocked:
            print(f"Roblox blocked API access ({result.status}): {blocked}")
            print("Stopped. Wait until that block lifts before running this again.")
            _log(f"api-block status={result.status} {blocked}")
            state["last_status"] = result.status
            _save_state(state)
            return 1

        if result.status == 401:
            print("The cookie expired. Paste a new .ROBLOSECURITY value into .env and start again.")
            _log("cookie expired")
            return 1

        if not result.ok:
            failures += 1
            print(f"Skipped \"{outfit['name']}\" ({outfit['id']}): {result.status} {result.message}")
            _log(f"fail id={outfit['id']} status={result.status} {result.message}")
            if failures >= len(selected):
                print("Every outfit in the cycle failed. Stopped so this does not keep requesting.")
                return 1
        else:
            failures = 0
            print(f"Equipped \"{outfit['name']}\" ({outfit['id']}). Next change in {interval:.0f}s.")
            _log(f"equipped id={outfit['id']} name={outfit['name']} status={result.status}")
            state["last_outfit_id"] = outfit["id"]
            state["last_outfit_name"] = outfit["name"]
            state["last_status"] = result.status

        state["next_index"] = (index + 1) % len(selected)
        state["interval_seconds"] = interval
        _save_state(state)

        extra = result.wait_seconds if result.ok else 0
        sleep_for = max(interval, extra)
        if extra > interval:
            print(f"Rate-limit budget is empty. Waiting {sleep_for:.0f}s before the next outfit.")
            _log(f"budget-wait {sleep_for:.0f}s")
        _sleep(sleep_for)


def _record_rate_limit(
    result: Any,
    state: dict[str, Any],
    config: dict[str, Any],
    interval: float,
    outfit: dict[str, Any],
) -> str:
    """Remember the 429, lengthen the gap, and either wait or ask the caller to exit.

    The same outfit stays next. A failed equip should be retried after the pause,
    not skipped.
    """
    hits = int(state.get("rate_limit_hits") or 0) + 1
    fallback = min(60.0 * (2 ** min(hits, 4)), float(config["max_interval_seconds"]))
    pause_for = result.retry_after if result.retry_after is not None else fallback
    new_interval = slowed_interval(interval, config["slowdown_factor"], config["max_interval_seconds"])
    state["rate_limit_hits"] = hits
    state["slowed"] = True
    state["interval_seconds"] = new_interval
    state["cooldown_until"] = time.time() + pause_for
    state["last_status"] = 429
    _save_state(state)
    message = (
        f"Rate limited while equipping \"{outfit['name']}\". "
        f"Pausing {pause_for:.0f}s and raising the interval from {interval:.0f}s to {new_interval:.0f}s."
    )
    print(message)
    _log(message)
    if config["on_rate_limit"] == "stop":
        print("on_rate_limit is stop, so this process is exiting. Raise interval_seconds, then run it again.")
        return "stop"
    _sleep(pause_for)
    state["cooldown_until"] = None
    _save_state(state)
    print(f"Resuming at {new_interval:.0f}s between outfits.")
    _log(f"resume interval={new_interval:.0f}s")
    return "pause"


def _wait_out_cooldown(state: dict[str, Any]) -> None:
    until = state.get("cooldown_until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return
    remaining = float(until) - time.time()
    if remaining > 0:
        print(f"Still in a rate-limit pause. Waiting {remaining:.0f}s.")
        _sleep(remaining)
    state["cooldown_until"] = None
    _save_state(state)


def _prepare_outfits(client: RobloxClient, user_id: int, config: dict[str, Any]) -> dict[str, Any]:
    live = client.list_outfits(user_id)
    avatar_outfits = [item for item in live if is_avatar_outfit(item)]
    if not avatar_outfits and live:
        print("No outfits were labeled Avatar. Using the full saved list.")
        avatar_outfits = live
    saved_doc = load_json(ROOT / "outfits.json", {})
    saved = saved_doc.get("outfits") if isinstance(saved_doc, dict) else None
    merged = merge_outfits(avatar_outfits, saved if isinstance(saved, list) else None)
    save_json(ROOT / "outfits.json", {"outfits": merged})
    selected = select_outfits(merged, config["outfit_ids"])
    missing = [outfit_id for outfit_id in config["outfit_ids"] if outfit_id not in {item["id"] for item in merged}]
    if missing:
        print("These outfit ids are not on this account and were skipped: " + ", ".join(str(item) for item in missing))
    return {"merged": merged, "selected": selected}


def _print_outfits(merged: list[dict[str, Any]], selected: list[dict[str, Any]]) -> None:
    selected_ids = {item["id"] for item in selected}
    if not merged:
        print("No saved outfits yet.")
        print("On roblox.com, open Avatar and save at least two outfits, then run this again.")
        return
    print()
    print(f"{'ID':<14}{'Name':<32}In cycle")
    print(f"{'-' * 14}{'-' * 32}--------")
    for outfit in merged:
        flag = "yes" if outfit["id"] in selected_ids else "no"
        name = outfit["name"]
        if len(name) > 30:
            name = name[:29] + "…"
        print(f"{outfit['id']:<14}{name:<32}{flag}")
    print()
    print(f"{len(selected)} of {len(merged)} outfits will cycle.")
    print("Edit outfits.json and set \"enabled\" to false for any look you want to leave out.")
    print("Or put specific ids in config.json under outfit_ids.")


def _config_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_file():
        return path
    fallback = ROOT / raw
    if fallback.is_file():
        return fallback
    return path


def _cookie() -> str:
    import os

    from_env = normalize_cookie(os.environ.get("ROBLOSECURITY", ""))
    if from_env.startswith("_|WARNING"):
        return from_env
    path = ROOT / ".env"
    if not path.is_file():
        return ""
    return cookie_from_text(path.read_text(encoding="utf-8-sig"))


def _load_state() -> dict[str, Any]:
    payload = load_json(ROOT / "state.json", {})
    return payload if isinstance(payload, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json(ROOT / "state.json", state)


def _log(message: str) -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (logs / "cycler.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {message}\n")


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
