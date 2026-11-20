"""Authenticated calls to the Roblox avatar endpoints for the signed-in user."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from robloxprofilemanager.logic import (
    OWNED_SCAN_TYPES,
    add_owned_asset,
    header_wait_seconds,
    parse_badge_id,
    parse_retry_after,
    roblox_error_text,
    v4_wear_body,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class RobloxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


@dataclass
class CallResult:
    ok: bool
    status: int
    message: str
    rate_limited: bool = False
    retry_after: float | None = None
    wait_seconds: float = 0.0
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    removed_ids: list[int] = field(default_factory=list)


class RobloxClient:
    def __init__(self, cookie: str, timeout: float):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Accept"] = "application/json"
        self.session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        self.user_id: int | None = None
        self._wear_route_retired = False
        self._outfit_bodies: dict[int, dict[str, Any]] = {}

    def close(self) -> None:
        self.session.close()

    def whoami(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                "https://users.roblox.com/v1/users/authenticated",
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RobloxError(
                "Could not reach Roblox. Check the connection and try again.",
                transient=True,
            ) from exc
        if response.status_code != 200:
            raise RobloxError(
                "Roblox rejected the cookie. Copy a fresh .ROBLOSECURITY value into .env.",
                response.status_code,
            )
        payload = _json_or_empty(response)
        if not isinstance(payload, dict) or not payload.get("id") or not payload.get("name"):
            raise RobloxError("Roblox did not return the signed-in user.")
        self.user_id = int(payload["id"])
        return payload

    def list_outfits(self, user_id: int) -> list[dict[str, Any]]:
        outfits: list[dict[str, Any]] = []
        page = 1
        token = ""
        while page <= 20:
            params: dict[str, Any] = {"itemsPerPage": 50, "page": page}
            if token:
                params["paginationToken"] = token
            result = self._request(
                "GET",
                f"https://avatar.roblox.com/v2/avatar/users/{user_id}/outfits",
                params=params,
            )
            if result.rate_limited:
                raise RobloxError(
                    "Roblox rate limited the outfit list. Wait a few minutes, then run this again.",
                    429,
                )
            if not result.ok or not isinstance(result.payload, dict):
                raise RobloxError(
                    f"Could not list outfits ({result.status}): {result.message}",
                    result.status,
                )
            batch = result.payload.get("data") or []
            if not isinstance(batch, list) or not batch:
                break
            outfits.extend(item for item in batch if isinstance(item, dict))
            token = str(result.payload.get("paginationToken") or "")
            if len(batch) < 50:
                break
            page += 1
        return outfits

    def wear_outfit(self, outfit_id: int) -> CallResult:
        """Wear a saved outfit with one request.

        The older wear route does that by itself. Rebuilding the avatar reads
        the outfit, patches it, reads it back, and redraws the thumbnail, which
        spends the rate limit four times per change. That path is only used
        after Roblox removes the wear route.
        """
        if not self._wear_route_retired:
            worn = self._request(
                "POST",
                f"https://avatar.roblox.com/v1/outfits/{outfit_id}/wear",
            )
            if worn.ok or worn.rate_limited or worn.status not in (404, 410):
                return worn
            self._wear_route_retired = True
        return self._wear_with_saved_body(outfit_id)

    def _wear_with_saved_body(self, outfit_id: int) -> CallResult:
        body = self._outfit_bodies.get(outfit_id)
        if body is None:
            details = self._request(
                "GET",
                f"https://avatar.roblox.com/v4/outfits/{outfit_id}/details",
            )
            if details.rate_limited:
                return details
            if not details.ok or not isinstance(details.payload, dict):
                details.ok = False
                details.message = details.message or "Could not read that outfit."
                return details
            body = v4_wear_body(details.payload)
            assets = body["avatarDefinition"]["updateAvatarModel"].get("assets") or []
            if not assets:
                return CallResult(
                    ok=False,
                    status=details.status,
                    message="That outfit has no wearable assets, so it was skipped.",
                    payload=details.payload,
                )
            self._outfit_bodies[outfit_id] = body
        result = self._request("PATCH", "https://avatar.roblox.com/v4/avatar", json_body=body)
        if result.ok and isinstance(result.payload, dict) and result.payload.get("success") is False:
            invalid = result.payload.get("invalidAssets") or []
            result.ok = False
            result.message = f"Roblox refused some items in this outfit ({len(invalid)} invalid)."
        return result

    def current_avatar(self) -> CallResult:
        return self._request("GET", "https://avatar.roblox.com/v1/avatar")

    def avatar_rules(self) -> CallResult:
        return self._request("GET", "https://avatar.roblox.com/v1/avatar-rules")

    def list_owned_assets(self, user_id: int) -> tuple[dict[int, list[int]], CallResult | None]:
        """One page per wearable type. A 429 stops the scan and returns what was found."""
        grouped: dict[int, list[int]] = {}
        for asset_type in OWNED_SCAN_TYPES:
            result = self._request(
                "GET",
                f"https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type}",
                params={"limit": 100, "sortOrder": "Desc"},
            )
            if result.rate_limited:
                return grouped, result
            if not result.ok or not isinstance(result.payload, dict):
                continue
            for item in result.payload.get("data") or []:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("assetId", item.get("id"))
                if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                    add_owned_asset(grouped, asset_type, raw_id)
        return grouped, None

    def wear_body(self, body: dict[str, Any], fallback: dict[str, Any] | None = None) -> CallResult:
        """Apply an avatar. Items Roblox refuses are removed and the rest is sent again."""
        removed: list[int] = []
        result = _mark_invalid_assets(self._request("PATCH", "https://avatar.roblox.com/v4/avatar", json_body=body))
        if not result.ok and not result.rate_limited and result.status != 401:
            removed.extend(_invalid_asset_ids(result))
            stripped = _without_assets(body, set(removed))
            if stripped != body:
                result = _mark_invalid_assets(
                    self._request("PATCH", "https://avatar.roblox.com/v4/avatar", json_body=stripped)
                )
                removed.extend(_invalid_asset_ids(result))
        if not result.ok and not result.rate_limited and result.status != 401 and fallback is not None:
            plain = _without_assets(fallback, set(removed))
            if plain != body:
                result = _mark_invalid_assets(
                    self._request("PATCH", "https://avatar.roblox.com/v4/avatar", json_body=plain)
                )
                removed.extend(_invalid_asset_ids(result))
        result.removed_ids = list(dict.fromkeys(removed))
        return result

    def set_description(self, description: str) -> CallResult:
        text = description.replace("\n", "").replace("\r", "")[:1000]
        return self._request(
            "POST",
            "https://users.roblox.com/v1/description",
            json_body={"description": text},
        )

    def list_badge_ids(self, user_id: int) -> CallResult:
        """Every awarded badge id. Roblox pages these; there is no clear-all call."""
        found: list[int] = []
        cursor = ""
        for _ in range(40):
            params: dict[str, Any] = {"limit": 100, "sortOrder": "Asc"}
            if cursor:
                params["cursor"] = cursor
            result = self._request(
                "GET",
                f"https://badges.roblox.com/v1/users/{user_id}/badges",
                params=params,
            )
            if not result.ok or not isinstance(result.payload, dict):
                return result
            for item in result.payload.get("data") or []:
                if isinstance(item, dict):
                    badge_id = parse_badge_id(item.get("id"))
                    if badge_id is not None:
                        found.append(badge_id)
            cursor = str(result.payload.get("nextPageCursor") or "")
            if not cursor:
                break
        return CallResult(ok=True, status=200, message="", payload=found)

    def delete_badge(self, badge_id: int) -> CallResult:
        return self._request("DELETE", f"https://badges.roblox.com/v1/user/badges/{badge_id}")

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retry_csrf: bool = True,
    ) -> CallResult:
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RobloxError(
                "Could not reach Roblox. Check the connection and try again.",
                transient=True,
            ) from exc
        if response.status_code == 403 and retry_csrf and _needs_csrf(response):
            self._refresh_csrf(response)
            return self._request(
                method,
                url,
                params=params,
                json_body=json_body,
                retry_csrf=False,
            )
        return _to_result(response)

    def _refresh_csrf(self, previous: requests.Response) -> None:
        token = previous.headers.get("x-csrf-token")
        if not token:
            try:
                probe = self.session.post("https://auth.roblox.com/v2/logout", timeout=self.timeout)
            except requests.RequestException as exc:
                raise RobloxError(
                    "Could not reach Roblox. Check the connection and try again.",
                    transient=True,
                ) from exc
            token = probe.headers.get("x-csrf-token")
        if not token:
            raise RobloxError(
                "Roblox did not issue an X-CSRF-TOKEN. The cookie is probably expired.",
                403,
            )
        self.session.headers["x-csrf-token"] = token


def _needs_csrf(response: requests.Response) -> bool:
    if response.headers.get("x-csrf-token"):
        return True
    text = response.text.lower()
    return "token validation" in text or "x-csrf-token" in text


def _json_or_empty(response: requests.Response) -> Any:
    if not response.text.strip():
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _invalid_asset_ids(result: CallResult) -> list[int]:
    payload = result.payload if isinstance(result.payload, dict) else {}
    raw = payload.get("invalidAssets") or payload.get("invalidAssetIds") or []
    if not isinstance(raw, list):
        return []
    found: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            found.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool):
            found.append(item["id"])
    return found


def _without_assets(body: dict[str, Any], banned: set[int]) -> dict[str, Any]:
    if not banned:
        return body
    model = body.get("avatarDefinition", {}).get("updateAvatarModel", {})
    assets = model.get("assets")
    if not isinstance(assets, list):
        return body
    kept = [asset for asset in assets if not (isinstance(asset, dict) and asset.get("id") in banned)]
    if len(kept) == len(assets):
        return body
    copy = {
        "updateTypes": list(body.get("updateTypes") or []),
        "avatarDefinition": {"updateAvatarModel": {key: value for key, value in model.items() if key != "assets"}},
    }
    if kept:
        copy["avatarDefinition"]["updateAvatarModel"]["assets"] = kept
    return copy


def _mark_invalid_assets(result: CallResult) -> CallResult:
    if not result.ok or not isinstance(result.payload, dict):
        return result
    if result.payload.get("success") is False:
        invalid = result.payload.get("invalidAssets") or result.payload.get("invalidAssetIds") or []
        result.ok = False
        count = len(invalid) if isinstance(invalid, list) else 0
        result.message = result.message or f"Roblox refused some items ({count} invalid)."
    return result


def _to_result(response: requests.Response) -> CallResult:
    now = time.time()
    payload = _json_or_empty(response)
    message = roblox_error_text(payload, response.text)
    rate_limited = response.status_code == 429
    retry_after = parse_retry_after(response.headers.get("Retry-After"), now) if rate_limited else None
    wait_seconds = header_wait_seconds(response.headers, now)
    headers = {key.lower(): value for key, value in response.headers.items()}
    return CallResult(
        ok=200 <= response.status_code < 300,
        status=response.status_code,
        message=message,
        rate_limited=rate_limited,
        retry_after=retry_after,
        wait_seconds=wait_seconds,
        payload=payload,
        headers=headers,
    )
