"""Command line entry for listing outfits and cycling them."""

from __future__ import annotations

import argparse
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from robloxprofilemanager.api import RobloxClient, RobloxError
from robloxprofilemanager.logic import (
    BADGE_DELETE_SECONDS,
    BADGE_IDLE_SECONDS,
    BIO_START_SECONDS,
    MIN_INTERVAL_SECONDS,
    active_interval,
    adapt_bio_pace,
    api_block_reason,
    badge_pause_seconds,
    absorb_wearables,
    add_owned_asset,
    cookie_from_text,
    freaky_avatar,
    glitch_bio,
    is_avatar_outfit,
    parse_avatar_rules,
    load_dotenv,
    load_json,
    merge_outfits,
    normalize_cookie,
    optional_seconds,
    prepared_bio_gap,
    read_config,
    save_json,
    select_outfits,
    slowed_interval,
)

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Manage your own Roblox profile: cycle outfits and refresh the About line.",
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
        if config["avatar_mode"] != "random" and len(selected) < 2:
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
    state["bio_next_at"] = None
    state["bio_interval_seconds"] = None
    state["bio_blocked_seconds"] = None
    state["badge_next_at"] = None
    state["interval_seconds"] = active_interval(
        config["interval_seconds"],
        {"slowed": False},
        config["max_interval_seconds"],
    )
    _save_state(state)
    print(
        f"Cleared the rate-limit slowdown. Outfits use {state['interval_seconds']:.0f}s from config.json. "
        f"The bio waits {BIO_START_SECONDS:.0f}s."
    )
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
    saved_bio = optional_seconds(state.get("bio_interval_seconds"))
    gap, blocked = prepared_bio_gap(
        saved_bio,
        optional_seconds(state.get("bio_blocked_seconds")),
        config["max_interval_seconds"],
    )
    state["bio_interval_seconds"] = gap
    state["bio_blocked_seconds"] = blocked
    due = optional_seconds(state.get("bio_next_at"))
    earliest = time.time() + gap
    if due is None or due < earliest:
        state["bio_next_at"] = earliest
    _save_state(state)
    print(f"Bio changes every {gap:.0f}s, slower than the outfit cycle, so description updates stay under the limit.")
    random_mode = config["avatar_mode"] == "random"
    wardrobe: dict[str, Any] | None = None
    if random_mode:
        print(f"Random avatar test. A new look every {interval:.0f}s. Ctrl+C stops it.")
        print('Set "avatar_mode" to "cycle" in config.json to go back to the saved outfits.')
        _log(f"start mode=random interval={interval:.0f}s")
        _notify(
            "Manager started",
            [f"Random avatar every {interval:.0f}s. Outfit cycle is paused."],
            color=0x5865F2,
        )
    else:
        print(f"Cycling {len(selected)} outfits every {interval:.0f}s. Ctrl+C stops it.")
        _log(f"start count={len(selected)} interval={interval:.0f}s")
        _notify(
            "Manager started",
            [f"Cycling {len(selected)} outfits every {interval:.0f}s."],
            color=0x5865F2,
        )
    if config["remove_badges"]:
        print(
            f"Owned badges are removed one every {BADGE_DELETE_SECONDS:.0f}s. "
            f"With none left, the list is checked every {BADGE_IDLE_SECONDS / 3600:.0f}h."
        )

    failures = 0
    while True:
        _wait_out_cooldown(state)
        interval = active_interval(
            config["interval_seconds"],
            state,
            config["max_interval_seconds"],
        )
        if random_mode:
            if wardrobe is None:
                try:
                    wardrobe = _load_wardrobe(client)
                except RobloxError as exc:
                    if exc.transient:
                        print(f"{exc} Waiting {interval:.0f}s.")
                        _log(f"network-wait {exc}")
                        _sleep(interval)
                        continue
                    print(exc)
                    _log(f"auth-stop {exc}")
                    return 1
            look = freaky_avatar(wardrobe["assets"], wardrobe["rules"], random.SystemRandom())
            outfit = {"id": 0, "name": _look_label(look)}
            try:
                result = client.wear_body(look["body"], look["fallback"])
                _forget_assets(wardrobe, result.removed_ids)
            except RobloxError as exc:
                if exc.transient:
                    print(f"{exc} Waiting {interval:.0f}s.")
                    _log(f"network-wait {exc}")
                    _sleep(interval)
                    continue
                print(exc)
                _log(f"auth-stop {exc}")
                return 1
            index = 0
        else:
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
            _notify(
                "API blocked",
                [f"Status: {result.status}", blocked, "The manager stopped."],
                color=0xED4245,
            )
            state["last_status"] = result.status
            _save_state(state)
            return 1

        if result.status == 401:
            print("The cookie expired. Paste a new .ROBLOSECURITY value into .env and start again.")
            _log("cookie expired")
            _notify("Cookie expired", ["Paste a new .ROBLOSECURITY value into .env."], color=0xED4245)
            return 1

        if not result.ok:
            failures += 1
            print(f"Skipped \"{outfit['name']}\" ({outfit['id']}): {result.status} {result.message}")
            _log(f"fail id={outfit['id']} status={result.status} {result.message}")
            _notify(
                "Outfit skipped",
                [
                    f"Outfit: {outfit['name']}",
                    f"ID: {outfit['id']}",
                    f"Status: {result.status}",
                    str(result.message)[:300],
                ],
                color=0xED4245,
            )
            limit = max(len(selected), 1) if not random_mode else 5
            if failures >= limit:
                print("The avatar update failed too many times. Stopped so this does not keep requesting.")
                _notify("Manager stopped", ["The avatar update failed too many times."], color=0xED4245)
                return 1
        else:
            failures = 0
            if random_mode:
                print(f"Applied a random avatar. Next change in {interval:.0f}s.")
                _log(f"random-avatar status={result.status} {outfit['name']}")
                _notify(
                    "Random avatar",
                    [outfit["name"], f"Status: {result.status}", f"Next change: {interval:.0f}s", "Rate limited: no"],
                    color=0x57F287,
                )
            else:
                print(f"Equipped \"{outfit['name']}\" ({outfit['id']}). Next change in {interval:.0f}s.")
                _log(f"equipped id={outfit['id']} name={outfit['name']} status={result.status}")
                _notify(
                    "Outfit swapped",
                    [
                        f"Outfit: {outfit['name']}",
                        f"ID: {outfit['id']}",
                        f"Status: {result.status}",
                        f"Next swap: {interval:.0f}s",
                        "Rate limited: no",
                    ],
                    color=0x57F287,
                )
            state["last_outfit_id"] = outfit["id"]
            state["last_outfit_name"] = outfit["name"]
            state["last_status"] = result.status

        if selected and not random_mode:
            state["next_index"] = (index + 1) % len(selected)
        state["interval_seconds"] = interval
        _save_state(state)

        bio_decision = _update_bio(client, state, config, interval)
        if bio_decision == "stop":
            return 1
        if bio_decision == "wait":
            continue

        if config["remove_badges"]:
            badge_decision = _update_badges(client, state)
            if badge_decision == "stop":
                return 1

        extra = result.wait_seconds if result.ok else 0
        sleep_for = max(interval, extra)
        if extra > interval:
            print(f"Rate-limit budget is empty. Waiting {sleep_for:.0f}s before the next outfit.")
            _log(f"budget-wait {sleep_for:.0f}s")
            _notify(
                "Rate limit budget empty",
                [f"Waiting {sleep_for:.0f}s before the next outfit.", "Rate limited: yes"],
                color=0xFEE75C,
            )
        _sleep(sleep_for)


def _load_wardrobe(client: RobloxClient) -> dict[str, Any]:
    """Rules plus owned wearables. The current avatar is kept even if inventory is limited."""
    rules_result = client.avatar_rules()
    if rules_result.rate_limited:
        raise RobloxError(
            "Roblox rate limited the avatar rules. Waiting, then trying again.",
            429,
            transient=True,
        )
    rules = parse_avatar_rules(rules_result.payload if rules_result.ok else {})
    if not rules["scales"]:
        rules["scales"] = {
            "height": {"min": 0.9, "max": 1.05, "increment": 0.01},
            "width": {"min": 0.7, "max": 1.0, "increment": 0.01},
            "head": {"min": 0.95, "max": 1.0, "increment": 0.01},
            "proportion": {"min": 0.0, "max": 1.0, "increment": 0.01},
            "bodyType": {"min": 0.0, "max": 1.0, "increment": 0.01},
        }
    current = client.current_avatar()
    if current.rate_limited:
        raise RobloxError(
            "Roblox rate limited the current avatar. Waiting, then trying again.",
            429,
            transient=True,
        )
    grouped: dict[int, list[int]] = {}
    if current.ok and isinstance(current.payload, dict):
        absorb_wearables(grouped, current.payload.get("assets"))
    if client.user_id is not None:
        owned, limited = client.list_owned_assets(client.user_id)
        for type_id, asset_ids in owned.items():
            for asset_id in asset_ids:
                add_owned_asset(grouped, type_id, asset_id)
        if limited is not None:
            print("Inventory scan was rate limited. Using the items already collected.")
            _log("inventory-rate-limit")
    if 27 not in grouped:
        raise RobloxError("Could not read the current body, so the avatar was left alone.")
    total = sum(len(asset_ids) for asset_ids in grouped.values())
    print(f"Randomizer loaded {total} owned items.")
    _log(f"wardrobe items={total}")
    return {"assets": grouped, "rules": rules}


def _forget_assets(wardrobe: dict[str, Any], asset_ids: list[int]) -> None:
    banned = set(asset_ids)
    if not banned:
        return
    grouped = wardrobe.get("assets")
    if not isinstance(grouped, dict):
        return
    for type_id, owned in list(grouped.items()):
        grouped[type_id] = [asset_id for asset_id in owned if asset_id not in banned]


def _look_label(look: dict[str, Any]) -> str:
    scales = look.get("scales") or {}
    scale_text = ", ".join(f"{name} {value:g}" for name, value in scales.items())
    colors = look.get("colors") or {}
    color_text = ", ".join(f"{name} {value}" for name, value in colors.items())
    return f"{look.get('asset_count', 0)} items; {scale_text}; {color_text}"


def _update_bio(
    client: RobloxClient,
    state: dict[str, Any],
    config: dict[str, Any],
    interval: float,
) -> str:
    """Set a new bio when its own timer is due.

    A description 429 lengthens only the bio gap and that slower pace stays.
    Outfit changes keep the interval from config.json.
    """
    now = time.time()
    due = state.get("bio_next_at")
    if isinstance(due, (int, float)) and not isinstance(due, bool) and now < float(due):
        return "ok"

    text = glitch_bio()
    try:
        result = client.set_description(text)
    except RobloxError as exc:
        if exc.transient:
            print(f"{exc}")
            _log(f"bio-network-wait {exc}")
            return "ok"
        print(exc)
        _log(f"auth-stop {exc}")
        _notify("Manager stopped", [str(exc)[:300]], color=0xED4245)
        return "stop"

    if result.rate_limited:
        gap_now = optional_seconds(state.get("bio_interval_seconds")) or interval
        pace = adapt_bio_pace(
            gap_now,
            accepted=False,
            maximum=config["max_interval_seconds"],
            retry_after=result.retry_after,
            header_wait=result.wait_seconds,
            blocked_seconds=optional_seconds(state.get("bio_blocked_seconds")),
        )
        _store_bio_pace(state, pace)
        message = (
            f"Rate limited while updating the bio. Next bio in {pace.wait_seconds:.0f}s, "
            f"and it stays there. Outfits stay at {interval:.0f}s."
        )
        print(message)
        _log(message)
        _notify(
            "Bio rate limited",
            [
                f"Next bio: {pace.wait_seconds:.0f}s",
                "That pace stays",
                f"Outfits stay at {interval:.0f}s",
                "Rate limited: yes",
            ],
            color=0xFEE75C,
        )
        return "ok"

    if result.status == 401:
        print("The cookie expired. Paste a new .ROBLOSECURITY value into .env and start again.")
        _log("cookie expired")
        _notify("Cookie expired", ["Paste a new .ROBLOSECURITY value into .env."], color=0xED4245)
        return "stop"

    if not result.ok:
        print(f"Bio was not changed: {result.status} {result.message}")
        _log(f"bio-fail status={result.status} {result.message}")
        _notify(
            "Bio failed",
            [f"Status: {result.status}", str(result.message)[:300]],
            color=0xED4245,
        )
        return "ok"

    current_gap = optional_seconds(state.get("bio_interval_seconds")) or BIO_START_SECONDS
    pace = adapt_bio_pace(
        current_gap,
        accepted=True,
        maximum=config["max_interval_seconds"],
        blocked_seconds=optional_seconds(state.get("bio_blocked_seconds")),
    )
    _store_bio_pace(state, pace)
    print(f"Updated bio ({len(text)} chars). Next bio in {pace.gap_seconds:.0f}s.")
    _log(f"bio status={result.status} len={len(text)} next={pace.gap_seconds:.0f}s")
    _notify(
        "Bio changed",
        [
            f"Length: {len(text)}",
            f"Next bio: {pace.gap_seconds:.0f}s",
            "Rate limited: no",
            text[:180],
        ],
        color=0x5865F2,
    )
    return "ok"


def _update_badges(client: RobloxClient, state: dict[str, Any]) -> str:
    """Remove owned badges one at a time. An empty account is checked rarely."""
    now = time.time()
    due = optional_seconds(state.get("badge_next_at"))
    if due is not None and now < due:
        return "ok"

    queue = _badge_queue(state)
    if not queue:
        if client.user_id is None:
            return "ok"
        try:
            listed = client.list_badge_ids(client.user_id)
        except RobloxError as exc:
            if exc.transient:
                print(f"{exc}")
                _log(f"badge-network-wait {exc}")
                state["badge_next_at"] = time.time() + BADGE_IDLE_SECONDS
                _save_state(state)
                return "ok"
            print(exc)
            _log(f"auth-stop {exc}")
            _notify("Manager stopped", [str(exc)[:300]], color=0xED4245)
            return "stop"
        if listed.rate_limited:
            return _badge_limited(state, listed.retry_after, listed.wait_seconds, "listing badges")
        if listed.status == 401:
            print("The cookie expired. Paste a new .ROBLOSECURITY value into .env and start again.")
            _log("cookie expired")
            _notify("Cookie expired", ["Paste a new .ROBLOSECURITY value into .env."], color=0xED4245)
            return "stop"
        if not listed.ok or not isinstance(listed.payload, list):
            print(f"Could not list badges: {listed.status} {listed.message}")
            _log(f"badge-list-fail status={listed.status} {listed.message}")
            state["badge_next_at"] = time.time() + BADGE_IDLE_SECONDS
            _save_state(state)
            return "ok"
        queue = [int(item) for item in listed.payload if isinstance(item, int) and item > 0]
        if not queue:
            state["badge_queue"] = []
            state["badge_next_at"] = time.time() + BADGE_IDLE_SECONDS
            if not state.get("badge_idle_announced"):
                state["badge_idle_announced"] = True
                message = f"No badges left. Next check in {BADGE_IDLE_SECONDS / 3600:.0f}h."
                print(message)
                _log(message)
                _notify("Badges clear", [message, "Rate limited: no"], color=0x57F287, webhook_env="DISCORD_BADGE_WEBHOOK_URL")
            else:
                print(f"No badges. Next check in {BADGE_IDLE_SECONDS / 3600:.0f}h.")
            _save_state(state)
            return "ok"
        state["badge_idle_announced"] = False
        state["badge_queue"] = list(queue)
        _save_state(state)
        message = (
            f"Found {len(queue)} badges. Removing one every {BADGE_DELETE_SECONDS:.0f}s. "
            "Roblox has no remove-all call."
        )
        print(message)
        _log(message)
        _notify("Removing badges", [message], color=0x5865F2, webhook_env="DISCORD_BADGE_WEBHOOK_URL")

    badge_id = queue.pop(0)
    try:
        result = client.delete_badge(badge_id)
    except RobloxError as exc:
        queue.insert(0, badge_id)
        state["badge_queue"] = queue
        if exc.transient:
            print(f"{exc}")
            _log(f"badge-network-wait {exc}")
            state["badge_next_at"] = time.time() + BADGE_DELETE_SECONDS
            _save_state(state)
            return "ok"
        print(exc)
        _log(f"auth-stop {exc}")
        _notify("Manager stopped", [str(exc)[:300]], color=0xED4245)
        _save_state(state)
        return "stop"

    if result.rate_limited:
        queue.insert(0, badge_id)
        state["badge_queue"] = queue
        return _badge_limited(state, result.retry_after, result.wait_seconds, f"removing badge {badge_id}")
    if result.status == 401:
        print("The cookie expired. Paste a new .ROBLOSECURITY value into .env and start again.")
        _log("cookie expired")
        _notify("Cookie expired", ["Paste a new .ROBLOSECURITY value into .env."], color=0xED4245)
        return "stop"
    if not result.ok:
        state["badge_queue"] = queue
        state["badge_next_at"] = time.time() + badge_pause_seconds(len(queue), limited=False)
        _save_state(state)
        print(f"Skipped badge {badge_id}: {result.status} {result.message}")
        _log(f"badge-skip id={badge_id} status={result.status} {result.message}")
        return "ok"

    state["badge_queue"] = queue
    pause = badge_pause_seconds(len(queue), limited=False)
    state["badge_next_at"] = time.time() + pause
    _save_state(state)
    if queue:
        message = f"Removed badge {badge_id}. {len(queue)} left. Next removal in {pause:.0f}s."
        print(message)
        _log(f"badge-removed id={badge_id} left={len(queue)}")
        _notify("Badge removed", [message, "Rate limited: no"], color=0x57F287, webhook_env="DISCORD_BADGE_WEBHOOK_URL")
        return "ok"
    message = f"Removed badge {badge_id}. No badges left. Next check in {pause / 3600:.0f}h."
    print(message)
    _log(message)
    state["badge_idle_announced"] = True
    _save_state(state)
    _notify("Badges clear", [message, "Rate limited: no"], color=0x57F287, webhook_env="DISCORD_BADGE_WEBHOOK_URL")
    return "ok"


def _badge_limited(state: dict[str, Any], retry_after: float | None, header_wait: float, action: str) -> str:
    pause = badge_pause_seconds(1, limited=True, retry_after=retry_after, header_wait=header_wait)
    state["badge_next_at"] = time.time() + pause
    _save_state(state)
    message = f"Rate limited while {action}. Next badge request in {pause:.0f}s."
    print(message)
    _log(message)
    _notify(
        "Badge rate limited",
        [message, "Rate limited: yes"],
        color=0xFEE75C,
        webhook_env="DISCORD_BADGE_WEBHOOK_URL",
    )
    return "ok"


def _badge_queue(state: dict[str, Any]) -> list[int]:
    raw = state.get("badge_queue")
    if not isinstance(raw, list):
        return []
    queue: list[int] = []
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            queue.append(item)
    return queue


def _store_bio_pace(state: dict[str, Any], pace: Any) -> None:
    state["bio_interval_seconds"] = pace.gap_seconds
    state["bio_blocked_seconds"] = pace.blocked_seconds
    state["bio_next_at"] = time.time() + pace.wait_seconds
    _save_state(state)


def _record_rate_limit(
    result: Any,
    state: dict[str, Any],
    config: dict[str, Any],
    interval: float,
    outfit: dict[str, Any],
    action: str | None = None,
) -> str:
    """Remember the 429, lengthen the gap, and either wait or ask the caller to exit.

    The same outfit stays next. A failed equip should be retried after the pause,
    not skipped.
    """
    hits = int(state.get("rate_limit_hits") or 0) + 1
    fallback = min(60.0 * (2 ** min(hits, 4)), float(config["max_interval_seconds"]))
    header_wait = float(result.wait_seconds or 0)
    if result.retry_after is not None:
        pause_for = max(result.retry_after, header_wait)
    else:
        pause_for = max(fallback, header_wait)
    new_interval = slowed_interval(interval, config["slowdown_factor"], config["max_interval_seconds"])
    state["rate_limit_hits"] = hits
    state["slowed"] = True
    state["interval_seconds"] = new_interval
    state["cooldown_until"] = time.time() + pause_for
    state["last_status"] = 429
    _save_state(state)
    message = (
        f"Rate limited while {action or 'equipping \"' + outfit['name'] + '\"'}. "
        f"Pausing {pause_for:.0f}s and raising the interval from {interval:.0f}s to {new_interval:.0f}s."
    )
    print(message)
    _log(message)
    _notify(
        "Outfit rate limited",
        [
            f"Outfit: {outfit['name']}",
            f"Pause: {pause_for:.0f}s",
            f"Interval: {interval:.0f}s -> {new_interval:.0f}s",
            "Rate limited: yes",
        ],
        color=0xFEE75C,
    )
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


def _notify(title: str, lines: list[str], *, color: int, webhook_env: str = "DISCORD_WEBHOOK_URL") -> None:
    """Post one status embed. A missing webhook, or a Discord error, does not stop the loop."""
    url = os.environ.get(webhook_env, "").strip()
    if not (
        url.startswith("https://discord.com/api/webhooks/")
        or url.startswith("https://discordapp.com/api/webhooks/")
    ):
        return
    payload = {
        "username": "RobloxProfileManager",
        "embeds": [
            {
                "title": title[:256],
                "description": "\n".join(lines)[:4000],
                "color": color,
            }
        ],
    }

    def _send() -> None:
        try:
            requests.post(url, json=payload, timeout=8)
        except requests.RequestException as exc:
            _log(f"discord-notify-failed {type(exc).__name__}")

    threading.Thread(target=_send, daemon=True).start()


def _cookie() -> str:
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
