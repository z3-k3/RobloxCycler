import unittest

from robloxprofilemanager.logic import (
    MIN_INTERVAL_SECONDS,
    active_interval,
    api_block_reason,
    avatar_type_value,
    body_colors_payload,
    clamp_interval,
    cookie_from_text,
    _BIO_STEMS,
    glitch_bio,
    header_wait_seconds,
    is_avatar_outfit,
    merge_outfits,
    normalize_cookie,
    parse_retry_after,
    relax_bio_gap,
    schedule_bio_after_limit,
    select_outfits,
    slowed_interval,
    v4_wear_body,
)


class CookieTests(unittest.TestCase):
    def test_strips_cookie_name_and_quotes(self) -> None:
        self.assertEqual(normalize_cookie('  ".ROBLOSECURITY=abc"  '), "abc")
        self.assertEqual(normalize_cookie("_|WARNING:-DO-NOT-SHARE-THIS.|_token"), "_|WARNING:-DO-NOT-SHARE-THIS.|_token")

    def test_cookie_on_the_following_line_is_used(self) -> None:
        text = "ROBLOSECURITY=\n_|WARNING:-DO-NOT-SHARE-THIS.|_token=\n"
        self.assertEqual(cookie_from_text(text), "_|WARNING:-DO-NOT-SHARE-THIS.|_token=")
        self.assertEqual(cookie_from_text("ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS.|_abc"), "_|WARNING:-DO-NOT-SHARE-THIS.|_abc")


class IntervalTests(unittest.TestCase):
    def test_floor_is_five_seconds(self) -> None:
        self.assertEqual(MIN_INTERVAL_SECONDS, 5)
        self.assertEqual(clamp_interval(0.5, 300), 5)
        self.assertEqual(clamp_interval(5, 300), 5)
        self.assertEqual(clamp_interval(60, 300), 60)
        self.assertEqual(clamp_interval(900, 300), 300)

    def test_rate_limit_slows_down_and_a_block_stops(self) -> None:
        self.assertEqual(slowed_interval(5, 2, 300), 10)
        self.assertIsNone(api_block_reason(429, "Too many requests"))
        self.assertIsNone(api_block_reason(403, "Token Validation Failed"))
        self.assertIsNone(api_block_reason(400, "Invalid assetId"))
        self.assertEqual(api_block_reason(403, "User is moderated"), "User is moderated")
        self.assertEqual(api_block_reason(403, ""), "HTTP 403")

    def test_successful_bio_returns_to_the_outfit_gap(self) -> None:
        self.assertEqual(relax_bio_gap(80, 10), 10)
        self.assertEqual(relax_bio_gap(10, 10), 10)

    def test_bio_limit_does_not_choose_the_outfit_gap(self) -> None:
        wait, gap = schedule_bio_after_limit(10, 5, 0, 300)
        self.assertEqual(gap, 20)
        self.assertEqual(wait, 20)
        wait, gap = schedule_bio_after_limit(20, 45, 0, 300)
        self.assertEqual(gap, 40)
        self.assertEqual(wait, 45)

    def test_slowdown_grows_and_caps(self) -> None:
        self.assertEqual(slowed_interval(60, 2, 300), 120)
        self.assertEqual(slowed_interval(240, 2, 300), 300)

    def test_saved_slowdown_sticks_until_config_is_slower(self) -> None:
        state = {"slowed": True, "interval_seconds": 120}
        self.assertEqual(active_interval(60, state, 300), 120)
        self.assertEqual(active_interval(180, state, 300), 180)
        self.assertEqual(active_interval(30, {"slowed": False}, 300), 30)


class RetryAfterTests(unittest.TestCase):
    def test_seconds_and_http_date(self) -> None:
        from email.utils import formatdate

        now = 1_500_000_000.0
        self.assertEqual(parse_retry_after("12", now), 12)
        self.assertEqual(parse_retry_after(formatdate(now + 90, usegmt=True), now), 90)

    def test_budget_wait_uses_reset_delta_or_unix_time(self) -> None:
        self.assertEqual(header_wait_seconds({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "40"}, 0), 40)
        self.assertEqual(header_wait_seconds({"x-ratelimit-remaining": "3", "x-ratelimit-reset": "40"}, 0), 0)
        self.assertEqual(
            header_wait_seconds({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000500"}, 1_700_000_000),
            500,
        )


class BioTests(unittest.TestCase):
    def test_each_bio_is_a_fresh_one_line_block(self) -> None:
        import random

        first = glitch_bio(rng=random.Random(1))
        second = glitch_bio(rng=random.Random(2))
        self.assertNotEqual(first, second)
        for text in (first, second):
            self.assertGreaterEqual(len(text), 500)
            self.assertLessEqual(len(text), 1000)
            self.assertNotIn("\n", text)
            self.assertNotIn("\r", text)
            self.assertIn("OPSEC", text[:90])
            self.assertTrue(any(char.isdigit() for char in text))
            self.assertIn("_", text)
            self.assertTrue(all(char.isalnum() or char == "_" for char in text))
            allowed = {stem.lower() for stem in _BIO_STEMS} | {"opsec", "xx"}
            words = []
            current = []
            for char in text:
                if char.isalpha():
                    current.append(char)
                elif current:
                    words.append("".join(current).lower())
                    current = []
            if current:
                words.append("".join(current).lower())
            self.assertTrue(words)
            self.assertTrue(all(word in allowed for word in words))


class OutfitTests(unittest.TestCase):
    def test_merge_keeps_enabled_flags_and_refreshes_names(self) -> None:
        live = [
            {"id": 1, "name": "Red", "outfitType": "Avatar"},
            {"id": 2, "name": "Blue", "outfitType": "Avatar"},
            {"id": 2, "name": "Blue duplicate", "outfitType": "Avatar"},
        ]
        saved = [{"id": 2, "name": "old", "enabled": False}]
        merged = merge_outfits(live, saved)
        self.assertEqual([item["id"] for item in merged], [1, 2])
        self.assertTrue(merged[0]["enabled"])
        self.assertFalse(merged[1]["enabled"])
        self.assertEqual(merged[1]["name"], "Blue")

    def test_config_ids_override_enabled_flags(self) -> None:
        merged = [
            {"id": 1, "name": "Red", "enabled": True},
            {"id": 2, "name": "Blue", "enabled": False},
        ]
        self.assertEqual([item["id"] for item in select_outfits(merged, [])], [1])
        self.assertEqual([item["id"] for item in select_outfits(merged, [2, 1])], [2, 1])

    def test_only_avatar_outfits_pass_the_filter(self) -> None:
        self.assertTrue(is_avatar_outfit({"outfitType": "Avatar"}))
        self.assertTrue(is_avatar_outfit({}))
        self.assertFalse(is_avatar_outfit({"outfitType": "DynamicHead"}))

    def test_v4_body_maps_legacy_outfit_fields(self) -> None:
        body = v4_wear_body(
            {
                "outfitModel": {
                    "playerAvatarType": "R15",
                    "scale": {"height": 1, "width": 1, "head": 1, "depth": 1, "proportion": 0, "bodyType": 0},
                    "bodyColor3s": {"headColor3": "#ffffff", "torsoColor3": "#111111"},
                    "assets": [{"id": 99, "name": "Hat", "meta": {"order": 1}}],
                }
            }
        )
        model = body["avatarDefinition"]["updateAvatarModel"]
        self.assertEqual(body["updateTypes"], [0, 2, 3, 4])
        self.assertEqual(model["playerAvatarType"], 3)
        self.assertEqual(model["scales"]["height"], 1)
        self.assertEqual(model["bodyColors"]["headColor"], "#ffffff")
        self.assertEqual(model["assets"], [{"id": 99, "meta": {"order": 1}}])
        self.assertEqual(avatar_type_value("R6"), 1)
        self.assertEqual(body_colors_payload({"headColor": "#abc"}), {"headColor": "#abc"})


if __name__ == "__main__":
    unittest.main()
