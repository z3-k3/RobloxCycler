"""Pure decisions for interval, cooldown, outfit selection, and bio pace."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

MIN_INTERVAL_SECONDS = 5
DEFAULT_MAX_INTERVAL_SECONDS = 300
BIO_START_SECONDS = 60.0
BIO_HOLD_RATIO = 1.15
BIO_HOLD_PAD = 8.0
BADGE_DELETE_SECONDS = 20.0
BADGE_IDLE_SECONDS = 2 * 60 * 60.0


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


@dataclass(frozen=True)
class BioPace:
    """How long to wait, the gap to keep, and the pace Roblox refused."""

    wait_seconds: float
    gap_seconds: float
    blocked_seconds: float | None


def optional_seconds(value: Any) -> float | None:
    """A state-file number, or None when the field is missing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def prepared_bio_gap(
    saved_gap: float | None,
    blocked: float | None,
    maximum: float,
) -> tuple[float, float | None]:
    """The bio gap to keep on startup. Never faster than the description floor."""
    gap = BIO_START_SECONDS if saved_gap is None else max(float(saved_gap), BIO_START_SECONDS)
    remembered = None
    if blocked is not None and float(blocked) > 0:
        remembered = max(float(blocked), BIO_START_SECONDS)
        gap = max(gap, remembered)
    gap = clamp_interval(gap, maximum)
    if remembered is not None:
        remembered = clamp_interval(remembered, maximum)
    return gap, remembered


def refusal_hold_seconds(floor: float, maximum: float) -> float:
    """A pace a little slower than the window Roblox just enforced."""
    base = max(float(floor), BIO_START_SECONDS)
    padded = max(base * BIO_HOLD_RATIO, base + BIO_HOLD_PAD)
    return clamp_interval(math.ceil(padded - 1e-9), maximum)


def adapt_bio_pace(
    current_gap: float,
    *,
    accepted: bool,
    maximum: float,
    retry_after: float | None = None,
    header_wait: float = 0.0,
    blocked_seconds: float | None = None,
) -> BioPace:
    """Keep description updates slower than the pace that gets a 429.

    The outfit interval is not the bio pace. A refusal becomes the new hold,
    with headroom past Retry-After, and a later success does not speed back up
    through that hold.
    """
    current = clamp_interval(max(float(current_gap), BIO_START_SECONDS), maximum)
    blocked = None
    if blocked_seconds is not None and float(blocked_seconds) > 0:
        blocked = clamp_interval(max(float(blocked_seconds), BIO_START_SECONDS), maximum)

    if not accepted:
        floor = max(current, float(retry_after or 0), float(header_wait or 0))
        if (retry_after is None or retry_after <= 0) and float(header_wait or 0) <= 0:
            floor = max(floor, slowed_interval(current, 2, maximum))
        gap = refusal_hold_seconds(floor, maximum)
        if blocked is not None:
            gap = clamp_interval(max(gap, blocked), maximum)
        return BioPace(wait_seconds=gap, gap_seconds=gap, blocked_seconds=gap)

    safe = blocked if blocked is not None else clamp_interval(BIO_START_SECONDS, maximum)
    if current > safe + 1:
        gap = clamp_interval(max(safe, safe + (current - safe) * 0.5), maximum)
    else:
        gap = clamp_interval(max(current, safe), maximum)
    return BioPace(wait_seconds=gap, gap_seconds=gap, blocked_seconds=blocked)


def badge_pause_seconds(
    remaining: int,
    *,
    limited: bool,
    retry_after: float | None = None,
    header_wait: float = 0.0,
) -> float:
    """One delete while badges remain. A long quiet check once they are gone."""
    if limited:
        return max(BADGE_DELETE_SECONDS, 60.0, float(retry_after or 0), float(header_wait or 0))
    if remaining <= 0:
        return BADGE_IDLE_SECONDS
    return BADGE_DELETE_SECONDS


def parse_badge_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def schedule_bio_after_limit(
    current_gap: float,
    retry_after: float | None,
    header_wait: float,
    maximum: float,
) -> tuple[float, float]:
    """Wait and the next bio gap after a description 429. The outfit gap stays put."""
    pace = adapt_bio_pace(
        current_gap,
        accepted=False,
        maximum=maximum,
        retry_after=retry_after,
        header_wait=header_wait,
    )
    return pace.wait_seconds, pace.gap_seconds


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

    mode = str(raw.get("avatar_mode", "cycle")).strip().lower()
    if mode not in {"cycle", "random"}:
        raise ConfigError('avatar_mode must be "cycle" or "random".')

    return {
        "interval_seconds": interval,
        "max_interval_seconds": max(maximum, MIN_INTERVAL_SECONDS),
        "slowdown_factor": factor,
        "on_rate_limit": action,
        "request_timeout_seconds": timeout,
        "outfit_ids": outfit_ids,
        "avatar_mode": mode,
        "remove_badges": bool(raw.get("remove_badges", False)),
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


# Asset type ids from Roblox avatar rules. Body parts stay at one each.
BODY_PART_TYPES = (27, 28, 29, 30, 31)
HEAD_TYPES = (79, 17)
STACK_TYPES = (8, 41, 42, 43, 44, 45, 46, 47, 64, 65, 66, 67, 68, 69, 72, 76, 77, 88, 89, 90)
OWNED_SCAN_TYPES = HEAD_TYPES + BODY_PART_TYPES + (18, 11, 12) + STACK_TYPES
# Far-apart entries from the public body-color palette. Nearby skin tones get rejected.
_HARSH_COLORS = (
    "#111111",
    "#F8F8F8",
    "#00FF00",
    "#FF0000",
    "#0000FF",
    "#FF00BF",
    "#FFFF00",
    "#00FFFF",
    "#AA00AA",
    "#FFAF00",
)
_COLOR_FIELDS = (
    "headColor",
    "torsoColor",
    "rightArmColor",
    "leftArmColor",
    "rightLegColor",
    "leftLegColor",
)
# Accessory types Roblox lets you slide, twist, and resize. Front and back tilt less on X.
_REFINE_TYPE_NAMES = {
    8: "Hat",
    42: "FaceAccessory",
    43: "NeckAccessory",
    44: "ShoulderAccessory",
    45: "FrontAccessory",
    46: "BackAccessory",
}
_LAYERED_TYPES = {64, 65, 66, 67, 68, 69, 72}
_DEFAULT_REFINE = {
    type_id: {
        "position": {"X": (-0.25, 0.25), "Y": (-0.25, 0.25), "Z": (-0.25, 0.25)},
        "rotation": {
            "X": (-15.0, 15.0) if type_id in {45, 46} else (-30.0, 30.0),
            "Y": (-30.0, 30.0),
            "Z": (-30.0, 30.0),
        },
        "scale": {"X": (0.8, 1.2), "Y": (0.8, 1.2), "Z": (0.8, 1.2)},
    }
    for type_id in _REFINE_TYPE_NAMES
}
_SCALE_PRESETS = (
    {"height": "max", "width": "min", "head": "max", "proportion": "min", "bodyType": "max"},
    {"height": "max", "width": "min", "head": "min", "proportion": "max", "bodyType": "min"},
    {"height": "min", "width": "max", "head": "max", "proportion": "max", "bodyType": "min"},
    {"height": "min", "width": "max", "head": "min", "proportion": "min", "bodyType": "max"},
    {"height": "max", "width": "max", "head": "max", "proportion": "min", "bodyType": "min"},
    {"height": "max", "width": "min", "head": "max", "proportion": "max", "bodyType": "max"},
)


def asset_type_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        raw = value.get("id")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def add_owned_asset(grouped: dict[int, list[int]], asset_type: int, asset_id: int) -> None:
    if asset_type <= 0 or asset_id <= 0:
        return
    bucket = grouped.setdefault(asset_type, [])
    if asset_id not in bucket:
        bucket.append(asset_id)


def absorb_wearables(grouped: dict[int, list[int]], assets: Any) -> None:
    if not isinstance(assets, list):
        return
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        raw_id = asset.get("id", asset.get("assetId"))
        type_id = asset_type_id(asset.get("assetType"))
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or type_id is None:
            continue
        add_owned_asset(grouped, type_id, raw_id)


def parse_avatar_rules(payload: Any) -> dict[str, Any]:
    """Pull the scale caps, slot limits, and allowed body colors out of avatar rules."""
    scales: dict[str, dict[str, float]] = {}
    limits: dict[int, int] = {}
    colors: list[str] = []
    if not isinstance(payload, dict):
        return {"scales": scales, "limits": limits, "colors": colors}
    raw_scales = payload.get("scales")
    if isinstance(raw_scales, dict):
        for name, spec in raw_scales.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            try:
                low = float(spec["min"])
                high = float(spec["max"])
                step = float(spec.get("increment") or 0.01)
            except (KeyError, TypeError, ValueError):
                continue
            if high < low or step <= 0:
                continue
            scales[name] = {"min": low, "max": high, "increment": step}
    for item in payload.get("wearableAssetTypes") or []:
        if not isinstance(item, dict):
            continue
        type_id = item.get("id")
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            continue
        try:
            limits[type_id] = max(0, int(item.get("maxNumber") or 0))
        except (TypeError, ValueError):
            continue
    for item in payload.get("bodyColorsPalette") or []:
        if isinstance(item, dict) and isinstance(item.get("hexColor"), str) and item["hexColor"].strip():
            colors.append(item["hexColor"].strip())
    return {"scales": scales, "limits": limits, "colors": colors, "refine": _parse_refine(payload)}


def asset_ids_from_outfit(payload: Any) -> set[int]:
    """Asset ids worn by one saved outfit. Used so the random look cannot rebuild it."""
    if not isinstance(payload, dict):
        return set()
    model = payload.get("outfitModel") if isinstance(payload.get("outfitModel"), dict) else payload
    assets = model.get("assets") if isinstance(model, dict) else None
    found: set[int] = set()
    if not isinstance(assets, list):
        return found
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        raw_id = asset.get("id", asset.get("assetId"))
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            found.add(raw_id)
    return found


def freaky_avatar(
    grouped: dict[int, list[int]],
    rules: dict[str, Any],
    rng: random.Random,
    blocked: set[int] | None = None,
) -> dict[str, Any]:
    """One legal-but-wrong avatar: extreme sliders, clashing colors, every slot filled.

    Roblox rejects scales outside avatar rules, so each slider is only min or max.
    Clothes and accessories from the saved outfits are left off. Body parts from
    those outfits are used only when the account has no other part of that type.
    """
    scales = _extreme_scales(rules.get("scales") or {}, rng)
    colors = _clashing_colors(rules.get("colors") or [], rng)
    limits = rules.get("limits") if isinstance(rules.get("limits"), dict) else {}
    refine = rules.get("refine") if isinstance(rules.get("refine"), dict) and rules.get("refine") else _DEFAULT_REFINE
    r6 = bool(grouped.get(17)) and rng.random() < 0.5
    full = _pick_assets(
        grouped,
        limits,
        rng,
        stacked=True,
        blocked=blocked or set(),
        refine=refine,
        r6=r6,
    )
    avatar_type = 1 if r6 else 3
    return {
        "body": _avatar_body(full, scales, colors, avatar_type),
        "fallback": _avatar_body(full, scales, colors, avatar_type),
        "scales": scales,
        "colors": colors,
        "asset_count": len(full),
    }


def _extreme_scales(scale_rules: dict[str, Any], rng: random.Random) -> dict[str, float]:
    preset = dict(rng.choice(_SCALE_PRESETS))
    chosen: dict[str, float] = {}
    for name, spec in scale_rules.items():
        if not isinstance(spec, dict):
            continue
        end = preset.get(name, rng.choice(("min", "max")))
        if rng.random() < 0.25:
            end = "min" if end == "max" else "max"
        try:
            chosen[name] = _snap_scale(spec, end)
        except (KeyError, TypeError, ValueError):
            continue
    proportion = chosen.get("proportion")
    if isinstance(proportion, float) and "bodyType" in chosen and "proportion" in scale_rules and "bodyType" in scale_rules:
        prop_spec = scale_rules["proportion"]
        body_spec = scale_rules["bodyType"]
        midpoint = (_snap_scale(prop_spec, "min") + _snap_scale(prop_spec, "max")) / 2
        chosen["bodyType"] = _snap_scale(body_spec, "min" if proportion >= midpoint else "max")
    return chosen


def _snap_scale(spec: dict[str, Any], end: str) -> float:
    low = float(spec["min"])
    high = float(spec["max"])
    step = float(spec.get("increment") or 0.01)
    if step <= 0:
        step = 0.01
    target = high if end == "max" else low
    steps = round((target - low) / step)
    value = min(high, max(low, low + steps * step))
    return round(value, 4)


def _clashing_colors(palette: list[str], rng: random.Random) -> dict[str, str]:
    allowed = {color.lower(): color for color in palette}
    pool = [allowed[color.lower()] for color in _HARSH_COLORS if color.lower() in allowed]
    if len(pool) < len(_COLOR_FIELDS):
        pool = list(palette)
    rng.shuffle(pool)
    if not pool:
        return {}
    return {field: pool[index % len(pool)] for index, field in enumerate(_COLOR_FIELDS)}


def _parse_refine(payload: dict[str, Any]) -> dict[int, dict[str, dict[str, tuple[float, float]]]]:
    lower = payload.get("accessoryRefinementLowerBounds")
    upper = payload.get("accessoryRefinementUpperBounds")
    if not isinstance(lower, dict) or not isinstance(upper, dict):
        return dict(_DEFAULT_REFINE)
    names = {name: type_id for type_id, name in _REFINE_TYPE_NAMES.items()}
    parsed: dict[int, dict[str, dict[str, tuple[float, float]]]] = {}
    for name, type_id in names.items():
        low = lower.get(name)
        high = upper.get(name)
        if not isinstance(low, dict) or not isinstance(high, dict):
            continue
        try:
            parsed[type_id] = {
                "position": _bound_pair(low, high, "position", ("xPosition", "X"), ("yPosition", "Y"), ("zPosition", "Z")),
                "rotation": _bound_pair(low, high, "rotation", ("xRotation", "X"), ("yRotation", "Y"), ("zRotation", "Z")),
                "scale": _bound_pair(low, high, "scale", ("xScale", "X"), ("yScale", "Y"), ("zScale", "Z")),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return parsed or dict(_DEFAULT_REFINE)


def _bound_pair(
    low: dict[str, Any],
    high: dict[str, Any],
    group: str,
    *axes: tuple[str, str],
) -> dict[str, tuple[float, float]]:
    low_group = low[group]
    high_group = high[group]
    if not isinstance(low_group, dict) or not isinstance(high_group, dict):
        raise TypeError(group)
    return {axis: (float(low_group[source]), float(high_group[source])) for source, axis in axes}


def _pick_assets(
    grouped: dict[int, list[int]],
    limits: dict[Any, Any],
    rng: random.Random,
    *,
    stacked: bool,
    blocked: set[int] | None = None,
    refine: dict[int, dict[str, dict[str, tuple[float, float]]]] | None = None,
    r6: bool = False,
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    used: set[int] = set()
    banned = blocked or set()
    bounds = refine or _DEFAULT_REFINE

    def take(type_id: int, count: int, *, allow_banned: bool) -> None:
        pool = [asset_id for asset_id in grouped.get(type_id, []) if asset_id not in used]
        fresh = [asset_id for asset_id in pool if asset_id not in banned]
        if fresh:
            pool = fresh
        elif not allow_banned:
            pool = []
        if count <= 0 or not pool:
            return
        rng.shuffle(pool)
        for asset_id in pool[:count]:
            used.add(asset_id)
            chosen.append(_cursed_asset(type_id, asset_id, rng, bounds))

    if r6:
        head_type = 17
    else:
        head_type = 79 if grouped.get(79) and (not grouped.get(17) or rng.random() < 0.75) else 17
        if not grouped.get(head_type):
            head_type = 17 if grouped.get(17) else 79
    take(head_type, 1, allow_banned=True)
    for type_id in BODY_PART_TYPES:
        take(type_id, 1, allow_banned=True)
    if rng.random() < 0.3:
        take(18, 1, allow_banned=True)
    if not stacked:
        return chosen
    if r6:
        take(8, limits.get(8, 3) if isinstance(limits.get(8, 3), int) else 3, allow_banned=False)
        return chosen
    for type_id in STACK_TYPES:
        cap = limits.get(type_id, 1)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = 1
        take(type_id, cap, allow_banned=False)
    return chosen


def _cursed_asset(
    type_id: int,
    asset_id: int,
    rng: random.Random,
    refine: dict[int, dict[str, dict[str, tuple[float, float]]]],
) -> dict[str, Any]:
    """Shove refinable items to a corner, puff layered clothes, and scramble dynamic heads."""
    meta: dict[str, Any] = {"version": 1}
    bounds = refine.get(type_id)
    if bounds:
        meta["position"] = _extreme_vector(rng, bounds["position"])
        meta["rotation"] = _extreme_vector(rng, bounds["rotation"])
        meta["scale"] = _extreme_vector(rng, bounds["scale"])
    elif type_id in _LAYERED_TYPES:
        meta["order"] = rng.randint(1, 8)
        meta["puffiness"] = 1
    elif type_id == 79:
        meta["headShape"] = rng.randrange(30)
        meta["staticFacialAnimation"] = False
    else:
        return {"id": asset_id}
    return {"id": asset_id, "meta": meta}


def _extreme_vector(rng: random.Random, axes: dict[str, tuple[float, float]]) -> dict[str, float]:
    return {axis: rng.choice(pair) for axis, pair in axes.items()}


def _avatar_body(
    assets: list[dict[str, Any]],
    scales: dict[str, float],
    colors: dict[str, str],
    avatar_type: int = 3,
) -> dict[str, Any]:
    model: dict[str, Any] = {"playerAvatarType": avatar_type}
    if scales and avatar_type != 1:
        model["scales"] = scales
    if colors:
        model["bodyColors"] = colors
    if assets:
        model["assets"] = assets
    return {"updateTypes": [0, 2, 3, 4], "avatarDefinition": {"updateAvatarModel": model}}


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


# Fragments Roblox's text filter commonly tags.
_BIO_BLOCKED = (
    "ass",
    "damn",
    "hell",
    "shit",
    "fuck",
    "bitch",
    "dick",
    "cock",
    "sex",
    "porn",
    "nude",
    "slut",
    "rape",
    "nazi",
    "kill",
    "hate",
    "hack",
    "cheat",
    "exploit",
    "robux",
    "scam",
    "discord",
    "bypass",
)


_PACKET_OPENERS = ("RBX_NET", "RBX_FRAME", "NET_TRACE", "FRAME_LINK", "RBX_LINK", "TRACE_NET")
_PACKET_FIELDS = (
    ("SESSION", "hex", 8),
    ("CLIENT_ID", "hex", 6),
    ("CHANNEL", "word", ("RELIABLE", "ORDERED")),
    ("PACKET_ID", "hex", 4),
    ("SEQUENCE", "int", 6),
    ("REPLICATION", "hex", 6),
    ("INSTANCE_REF", "hex", 6),
    ("STREAM", "word", ("DATA_MODEL", "CLIENT_REP")),
    ("PAYLOAD", "hex", 12),
    ("TIMESTAMP", "int", 10),
    ("ACK", "hex", 4),
    ("BUFFER", "hex", 8),
    ("STATE", "word", ("SYNC_PENDING", "SYNC_READY")),
    ("FLAGS", "hex", 2),
)
_BIO_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZx_:[]")


def glitch_bio(length: int | None = None, rng: random.Random | None = None) -> str:
    """One replication-log line. Field order and values change every call.

    `length` is accepted so older callers still work. The packet stays on one
    line and within 1000 characters, which is Roblox's description limit.
    One bio in a hundred is a deformed "Roblox tried to ban me."
    """
    picker = rng or random.SystemRandom()
    del length
    if picker.random() < 0.01:
        return _deformed_ban_line(picker)
    for _ in range(20):
        text = _packet_bio(picker)
        if _bio_is_safe(text):
            return text
    return _safe_bio(picker)


def _deformed_ban_line(rng: random.Random) -> str:
    """The same sentence, with case, doubled letters, and a few lookalike digits."""
    swaps = {"o": "0", "i": "1", "e": "3"}
    words: list[str] = []
    for word in ("Roblox", "tried", "to", "ban", "me"):
        chars: list[str] = []
        for char in word:
            rolled = char
            if rng.random() < 0.35 and char.lower() in swaps:
                rolled = swaps[char.lower()]
            elif rng.random() < 0.5:
                rolled = char.swapcase()
            chars.append(rolled)
            if rng.random() < 0.2:
                chars.append(rolled)
        words.append("".join(chars))
    return " ".join(words) + "."


def _packet_bio(rng: random.Random) -> str:
    required = {"SESSION", "CHANNEL", "PACKET_ID"}
    chosen = [field for field in _PACKET_FIELDS if field[0] in required]
    optional = [field for field in _PACKET_FIELDS if field[0] not in required]
    rng.shuffle(optional)
    chosen.extend(optional[: rng.randint(5, len(optional))])
    rng.shuffle(chosen)
    parts = [rng.choice(_PACKET_OPENERS)]
    for name, kind, spec in chosen:
        parts.append(f"{name}[{_packet_value(rng, kind, spec)}]")
    parts.append("END")
    return "::".join(parts)[:1000]


def _packet_value(rng: random.Random, kind: str, spec: Any) -> str:
    if kind == "hex":
        return "0x" + _hex(rng, int(spec))
    if kind == "int":
        width = int(spec)
        return f"{rng.randint(0, 10**width - 1):0{width}d}"
    return rng.choice(spec)


def _safe_bio(rng: random.Random) -> str:
    parts = [
        "RBX_NET",
        f"SESSION[0x{_hex(rng, 8)}]",
        f"CLIENT_ID[0x{_hex(rng, 6)}]",
        "CHANNEL[RELIABLE]",
        f"PACKET_ID[0x{_hex(rng, 4)}]",
        f"SEQUENCE[{rng.randint(0, 999999):06d}]",
        f"REPLICATION[0x{_hex(rng, 6)}]",
        f"STREAM[DATA_MODEL]",
        f"PAYLOAD[0x{_hex(rng, 12)}]",
        f"STATE[SYNC_PENDING]",
        "END",
    ]
    return "::".join(parts)


def _hex(rng: random.Random, digits: int) -> str:
    return "".join(rng.choice("0123456789ABCDEF") for _ in range(digits))


def _bio_is_safe(text: str) -> bool:
    if not text.startswith(_PACKET_OPENERS) or not text.endswith("::END"):
        return False
    if len(text) > 1000 or any(char not in _BIO_CHARS for char in text):
        return False
    lowered = text.lower()
    return not any(fragment in lowered for fragment in _BIO_BLOCKED)


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
