"""Pure decisions for interval, cooldown, and outfit selection."""

from __future__ import annotations

import json
import random
import string
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

MIN_INTERVAL_SECONDS = 5
DEFAULT_MAX_INTERVAL_SECONDS = 300


class ConfigError(ValueError):
    """config.json has a value this program will not run with."""


def normalize_cookie(raw: str) -> str:
    """Accept a bare cookie, a Cookie header, or a quoted value."""
    value = raw.strip().strip('"').strip("'")
    lowered = value.lower()
    if lowered.startswith(".roblosecurity="):
        value = value.split("=", 1)[1].strip()
    elif lowered.startswith("roblosecurity="):
        value = value.split("=", 1)[1].strip()
    return value


def cookie_from_text(text: str) -> str:
    """Find a Roblox cookie even when it sits on the line under ROBLOSECURITY=."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        if lowered.startswith("roblosecurity=") or lowered.startswith(".roblosecurity="):
            value = normalize_cookie(stripped)
            if value.startswith("_|WARNING"):
                return value
            for follow in lines[index + 1 :]:
                follow = follow.strip()
                if not follow or follow.startswith("#"):
                    continue
                found = normalize_cookie(follow)
                if found.startswith("_|WARNING"):
                    return found
                break
            continue
        found = normalize_cookie(stripped)
        if found.startswith("_|WARNING"):
            return found
    return ""


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into the environment without overriding real env vars."""
    import os

    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Return seconds to wait. Accepts a number of seconds or an HTTP date."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
        return max(0.0, moment.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


def header_wait_seconds(headers: Any, now: float) -> float:
    """Seconds to wait when the rate-limit budget is already spent."""
    remaining = _header(headers, "x-ratelimit-remaining")
    if remaining is None:
        return 0.0
    try:
        if int(float(remaining)) > 0:
            return 0.0
    except ValueError:
        return 0.0

    reset = _header(headers, "x-ratelimit-reset")
    if reset is None:
        return 0.0
    try:
        number = float(reset)
    except ValueError:
        parsed = parse_retry_after(reset, now)
        return parsed or 0.0
    if number > 1_000_000_000:
        return max(0.0, number - now)
    return max(0.0, number)


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        return None
    return str(value)


def api_block_reason(status: int, message: str) -> str | None:
    """Why the program should exit. A 429 is a timeout and is handled by slowing down."""
    text = (message or "").strip()
    lowered = text.lower()
    if status == 429 or "token validation" in lowered or "x-csrf-token" in lowered:
        return None
    if any(
        marker in lowered
        for marker in (
            "does not have access to the details",
            "useroutfitid is invalid",
            "invalid asset",
            "not wearable",
        )
    ):
        return None
    if status == 403 or any(
        marker in lowered
        for marker in (
            "banned",
            "moderated",
            "moderation",
            "restricted",
            "suspended",
            "suspension",
            "temporarily blocked",
            "temporarily disabled",
            "challenge is required",
            "authorization has been denied",
        )
    ):
        return text or f"HTTP {status}"
    return None


def clamp_interval(seconds: float, maximum: float) -> float:
    """Keep a requested interval inside the floor and the configured ceiling."""
    ceiling = max(float(maximum), MIN_INTERVAL_SECONDS)
    return min(ceiling, max(MIN_INTERVAL_SECONDS, float(seconds)))


def schedule_bio_after_limit(
    current_gap: float,
    retry_after: float | None,
    header_wait: float,
    maximum: float,
) -> tuple[float, float]:
    """Wait and the next bio gap after a description 429. The outfit gap stays put."""
    gap = slowed_interval(max(float(current_gap), MIN_INTERVAL_SECONDS), 2, maximum)
    pause = MIN_INTERVAL_SECONDS if retry_after is None else max(0.0, float(retry_after))
    wait = max(pause, float(header_wait or 0), gap)
    return wait, gap


def slowed_interval(current: float, factor: float, maximum: float) -> float:
    """Lengthen the gap after a 429. Never shorter than the current gap."""
    grown = max(float(current) * max(float(factor), 1.0), float(current) + MIN_INTERVAL_SECONDS)
    return clamp_interval(grown, maximum)


def active_interval(config_interval: float, state: dict[str, Any], maximum: float) -> float:
    """Use the slowed interval after a 429 until the config asks for something slower."""
    configured = clamp_interval(config_interval, maximum)
    if not state.get("slowed"):
        return configured
    saved = state.get("interval_seconds")
    if isinstance(saved, (int, float)) and not isinstance(saved, bool):
        return clamp_interval(max(configured, float(saved)), maximum)
    return configured


def load_json(path: Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_config(path: Path) -> dict[str, Any]:
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        raise ConfigError(f"Missing {path.name}. Keep the config.json that came with the repo.")

    try:
        interval = float(raw.get("interval_seconds", 60))
        maximum = float(raw.get("max_interval_seconds", DEFAULT_MAX_INTERVAL_SECONDS))
        factor = float(raw.get("slowdown_factor", 2))
        timeout = float(raw.get("request_timeout_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("interval, max interval, slowdown, and timeout must be numbers.") from exc

    if interval <= 0 or maximum <= 0 or timeout <= 0:
        raise ConfigError("interval, max interval, and timeout must be greater than 0.")
    if factor < 1:
        raise ConfigError("slowdown_factor must be at least 1.")

    action = str(raw.get("on_rate_limit", "pause")).strip().lower()
    if action not in {"pause", "stop"}:
        raise ConfigError('on_rate_limit must be "pause" or "stop".')

    outfit_ids = raw.get("outfit_ids", [])
    if not isinstance(outfit_ids, list) or not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in outfit_ids):
        raise ConfigError("outfit_ids must be a list of positive outfit id numbers, or [].")

    return {
        "interval_seconds": interval,
        "max_interval_seconds": max(maximum, MIN_INTERVAL_SECONDS),
        "slowdown_factor": factor,
        "on_rate_limit": action,
        "request_timeout_seconds": timeout,
        "outfit_ids": outfit_ids,
        "interval_was_raised": interval < MIN_INTERVAL_SECONDS,
    }


def avatar_type_value(value: Any) -> int | None:
    """Map outfit avatar type strings onto the v4 numbers (R6=1, R15=3)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in (1, 3):
        return value
    if isinstance(value, str):
        key = value.strip().upper()
        if key in {"1", "R6"}:
            return 1
        if key in {"3", "R15"}:
            return 3
    return None


def body_colors_payload(source: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(source, dict):
        return None
    pairs = (
        ("headColor", "headColor3"),
        ("torsoColor", "torsoColor3"),
        ("rightArmColor", "rightArmColor3"),
        ("leftArmColor", "leftArmColor3"),
        ("rightLegColor", "rightLegColor3"),
        ("leftLegColor", "leftLegColor3"),
    )
    colors: dict[str, str] = {}
    for modern, legacy in pairs:
        raw = source.get(modern, source.get(legacy))
        if isinstance(raw, str) and raw.strip():
            colors[modern] = raw.strip()
    return colors or None


def wear_assets(assets: Any) -> list[dict[str, Any]]:
    if not isinstance(assets, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id")
        if isinstance(asset_id, bool) or not isinstance(asset_id, int):
            continue
        item: dict[str, Any] = {"id": asset_id}
        meta = asset.get("meta")
        if isinstance(meta, dict):
            item["meta"] = meta
        cleaned.append(item)
    return cleaned


def v4_wear_body(details: dict[str, Any]) -> dict[str, Any]:
    """Build a PATCH /v4/avatar body from outfit details.

    The public docs number the update types 0 through 8 without naming them.
    Checked against the live avatar API: 2 is the body type, 3 is body colors,
    and 4 is the worn assets. Type 4 is the one that changes what the profile
    shows. Types 0, 2, and 3 travel with it so scale, body type, and colors
    follow the outfit.
    """
    model = details.get("outfitModel") if isinstance(details.get("outfitModel"), dict) else details
    avatar_model: dict[str, Any] = {}
    scales = model.get("scales", model.get("scale"))
    if isinstance(scales, dict):
        avatar_model["scales"] = scales
    avatar_type = avatar_type_value(model.get("playerAvatarType"))
    if avatar_type is not None:
        avatar_model["playerAvatarType"] = avatar_type
    colors = body_colors_payload(model.get("bodyColors") or model.get("bodyColor3s"))
    if colors:
        avatar_model["bodyColors"] = colors
    assets = wear_assets(model.get("assets"))
    if assets:
        avatar_model["assets"] = assets
    return {
        "updateTypes": [0, 2, 3, 4],
        "avatarDefinition": {"updateAvatarModel": avatar_model},
    }


def is_avatar_outfit(outfit: dict[str, Any]) -> bool:
    kind = outfit.get("outfitType")
    if kind is None or kind == "":
        return True
    return str(kind).strip().lower() in {"avatar", "0"}


def merge_outfits(live: list[dict[str, Any]], saved: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Refresh names from Roblox and keep the enabled flags the user already set."""
    enabled_by_id: dict[int, bool] = {}
    for item in saved or []:
        if not isinstance(item, dict):
            continue
        outfit_id = item.get("id")
        if isinstance(outfit_id, int) and not isinstance(outfit_id, bool):
            enabled_by_id[outfit_id] = bool(item.get("enabled", True))

    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for outfit in live:
        outfit_id = outfit.get("id")
        if not isinstance(outfit_id, int) or isinstance(outfit_id, bool) or outfit_id in seen:
            continue
        seen.add(outfit_id)
        name = outfit.get("name")
        merged.append(
            {
                "id": outfit_id,
                "name": name if isinstance(name, str) and name.strip() else f"Outfit {outfit_id}",
                "enabled": enabled_by_id.get(outfit_id, True),
            }
        )
    return merged


def select_outfits(merged: list[dict[str, Any]], outfit_ids: list[int]) -> list[dict[str, Any]]:
    """config outfit_ids wins. Otherwise every outfit still marked enabled."""
    by_id = {item["id"]: item for item in merged}
    if outfit_ids:
        chosen: list[dict[str, Any]] = []
        for outfit_id in outfit_ids:
            item = by_id.get(outfit_id)
            if item is not None:
                chosen.append(item)
        return chosen
    return [item for item in merged if item.get("enabled", True)]


_BIO_STEMS = (
    "SYSTEM",
    "UNKNOWN",
    "VOID",
    "ACCESS",
    "ERROR",
    "SIGNAL",
    "LOST",
    "NULL",
    "TRACE",
    "KERNEL",
    "BUFFER",
    "PACKET",
    "OFFSET",
    "SECTOR",
    "HASH",
    "NODE",
    "RELAY",
    "CIPHER",
    "GHOST",
    "STATIC",
    "FRAME",
    "DUMP",
    "STACK",
    "OPCODE",
    "SEGMENT",
    "CHECKSUM",
)
_BIO_SYMBOLS = "_|/\\#*+=~^[]<>"


def glitch_bio(length: int | None = None, rng: random.Random | None = None) -> str:
    """One line of fresh terminal noise, 500 to 1000 characters, with OPSEC near the start."""
    picker = rng or random.SystemRandom()
    target = picker.randint(500, 1000) if length is None else length
    target = min(1000, max(500, int(target)))
    chunks: list[str] = []
    built = 0
    while built < target:
        chunk = _glitch_chunk(picker)
        chunks.append(chunk)
        built += len(chunk)
    text = "".join(chunks)
    spot = picker.randint(0, 80)
    text = text[:spot] + "OPSEC" + text[spot:]
    text = text.replace("\n", "").replace("\r", "")
    if len(text) < target:
        text += _glitch_chunk(picker)
    return text[:target]


def _glitch_chunk(rng: random.Random) -> str:
    kind = rng.randrange(6)
    if kind == 0:
        left = _noise_token(rng, 3, 8)
        right = _noise_token(rng, 3, 8)
        wrap = rng.choice(("xX_", "_Xx"))
        return f"{wrap}{left}_{right}"
    if kind == 1:
        return f"{_cased_stem(rng)}_{_cased_stem(rng)}_{rng.randint(0, 9999):03d}"
    if kind == 2:
        return "NULL_" * rng.randint(2, 4) + f"{rng.randint(0, 99):02d}"
    if kind == 3:
        digits = "".join(rng.choice(string.digits) for _ in range(rng.randint(3, 6)))
        return f"ERROR_{digits}"
    if kind == 4:
        body = _noise_token(rng, 8, 24)
        return rng.choice(_BIO_SYMBOLS) + body + rng.choice(_BIO_SYMBOLS)
    return _cased_stem(rng) + rng.choice(_BIO_SYMBOLS) + _noise_token(rng, 2, 6)


def _noise_token(rng: random.Random, shortest: int, longest: int) -> str:
    alphabet = string.ascii_letters + string.digits + "_"
    count = rng.randint(shortest, longest)
    return "".join(rng.choice(alphabet) for _ in range(count))


def _cased_stem(rng: random.Random) -> str:
    word = rng.choice(_BIO_STEMS)
    styles = (
        word,
        word.lower(),
        word.capitalize(),
        "".join(rng.choice((char.lower(), char.upper())) for char in word),
    )
    return rng.choice(styles)


def roblox_error_text(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            messages = []
            for error in errors:
                if isinstance(error, dict) and error.get("message"):
                    messages.append(str(error["message"]))
            if messages:
                return "; ".join(messages)
        if payload.get("message"):
            return str(payload["message"])
    text = fallback.strip()
    return text[:300] if text else "Roblox returned an empty error."
