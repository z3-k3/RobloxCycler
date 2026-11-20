import unittest

from robloxprofilemanager.logic import (
    MIN_INTERVAL_SECONDS,
    active_interval,
    api_block_reason,
    avatar_type_value,
    body_colors_payload,
    clamp_interval,
    _BIO_BLOCKED,
    adapt_bio_pace,
    badge_pause_seconds,
    cookie_from_text,
    glitch_bio,
    header_wait_seconds,
    is_avatar_outfit,
    merge_outfits,
    normalize_cookie,
    parse_badge_id,
    parse_retry_after,
    prepared_bio_gap,
    schedule_bio_after_limit,
    select_outfits,
    slowed_interval,
    freaky_avatar,
    parse_avatar_rules,
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

    def test_successful_bio_holds_the_slow_floor(self) -> None:
        pace = adapt_bio_pace(10, accepted=True, maximum=300)
        self.assertEqual(pace.gap_seconds, 60)
        self.assertEqual(pace.wait_seconds, 60)
        self.assertIsNone(pace.blocked_seconds)

    def test_a_refusal_stays_slower_than_the_retry_window(self) -> None:
        pace = adapt_bio_pace(10, accepted=False, maximum=300, retry_after=44)
        self.assertGreaterEqual(pace.gap_seconds, 69)
        self.assertEqual(pace.wait_seconds, pace.gap_seconds)
        self.assertEqual(pace.blocked_seconds, pace.gap_seconds)
        held = adapt_bio_pace(
            pace.gap_seconds,
            accepted=True,
            maximum=300,
            blocked_seconds=pace.blocked_seconds,
        )
        self.assertEqual(held.gap_seconds, pace.gap_seconds)

    def test_a_long_retry_after_pushes_the_bio_further_out(self) -> None:
        pace = adapt_bio_pace(60, accepted=False, maximum=300, retry_after=90)
        self.assertGreaterEqual(pace.gap_seconds, 103.5)
        self.assertEqual(pace.wait_seconds, pace.gap_seconds)

    def test_startup_does_not_return_the_bio_to_the_outfit_gap(self) -> None:
        self.assertEqual(prepared_bio_gap(10, None, 300), (60, None))
        self.assertEqual(prepared_bio_gap(None, None, 300), (60, None))
        self.assertEqual(prepared_bio_gap(80, 10, 300), (80, 60))

    def test_bio_limit_does_not_choose_the_outfit_gap(self) -> None:
        wait, gap = schedule_bio_after_limit(10, 44, 0, 300)
        self.assertGreaterEqual(gap, 69)
        self.assertEqual(wait, gap)
        wait, gap = schedule_bio_after_limit(60, 90, 0, 300)
        self.assertGreaterEqual(gap, 103.5)
        self.assertEqual(wait, gap)

    def test_a_later_bio_limit_moves_the_safe_pace_out(self) -> None:
        pace = adapt_bio_pace(60, accepted=False, maximum=300, blocked_seconds=60, retry_after=44)
        self.assertGreaterEqual(pace.gap_seconds, 69)
        self.assertEqual(pace.blocked_seconds, pace.gap_seconds)

    def test_a_success_does_not_speed_back_through_the_hold(self) -> None:
        pace = adapt_bio_pace(60, accepted=True, maximum=300, blocked_seconds=69)
        self.assertGreaterEqual(pace.gap_seconds, 69)
        self.assertEqual(pace.blocked_seconds, 69)

    def test_badge_checks_back_off_once_the_list_is_empty(self) -> None:
        self.assertEqual(badge_pause_seconds(4, limited=False), 20)
        self.assertEqual(badge_pause_seconds(0, limited=False), 7200)
        self.assertEqual(badge_pause_seconds(3, limited=True, retry_after=44), 60)
        self.assertEqual(badge_pause_seconds(3, limited=True, retry_after=90), 90)
        self.assertEqual(parse_badge_id(12), 12)
        self.assertEqual(parse_badge_id("88"), 88)
        self.assertIsNone(parse_badge_id(True))
        self.assertIsNone(parse_badge_id(0))

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
    def test_each_bio_is_a_fresh_packet_line(self) -> None:
        import random

        samples = [glitch_bio(rng=random.Random(seed)) for seed in range(1, 13)]
        self.assertEqual(len(set(samples)), len(samples))
        self.assertGreaterEqual(len({text.split("::", 1)[0] for text in samples}), 3)
        for text in samples:
            self.assertLessEqual(len(text), 1000)
            self.assertGreater(len(text), 180)
            self.assertTrue(text.startswith(("RBX_NET::", "RBX_FRAME::", "NET_TRACE::", "FRAME_LINK::", "RBX_LINK::", "TRACE_NET::")))
            self.assertTrue(text.endswith("::END"))
            self.assertIn("::SESSION[0x", text)
            self.assertIn("::CHANNEL[", text)
            self.assertNotIn("\n", text)
            self.assertNotIn("\r", text)
            self.assertNotIn(" ", text)
            self.assertTrue(all(char.isalnum() or char in "_:[]" for char in text))
            lowered = text.lower()
            for fragment in _BIO_BLOCKED:
                self.assertNotIn(fragment, lowered)

    def test_one_percent_of_bios_are_the_deformed_line(self) -> None:
        import random

        class _Always(random.Random):
            def random(self) -> float:
                return 0.0

        forced = glitch_bio(rng=_Always())
        self.assertTrue(forced.endswith("."))
        self.assertEqual(len(forced.split(" ")), 5)
        hits = 0
        for seed in range(400):
            text = glitch_bio(rng=random.Random(seed))
            if text.endswith(".") and " " in text:
                hits += 1
                pieces = text[:-1].split(" ")
                self.assertEqual(len(pieces), 5)
        self.assertGreater(hits, 0)
        self.assertLess(hits, 20)


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


class FreakyAvatarTests(unittest.TestCase):
    def test_scales_hit_the_caps_and_slots_do_not_overflow(self) -> None:
        import random

        rules = parse_avatar_rules(
            {
                "scales": {
                    "height": {"min": 0.9, "max": 1.05, "increment": 0.01},
                    "width": {"min": 0.7, "max": 1.0, "increment": 0.01},
                    "head": {"min": 0.95, "max": 1.0, "increment": 0.01},
                    "proportion": {"min": 0.0, "max": 1.0, "increment": 0.01},
                    "bodyType": {"min": 0.0, "max": 1.0, "increment": 0.01},
                },
                "wearableAssetTypes": [
                    {"id": 8, "maxNumber": 3, "name": "Hat"},
                    {"id": 27, "maxNumber": 1, "name": "Torso"},
                    {"id": 41, "maxNumber": 1, "name": "Hair Accessory"},
                ],
                "bodyColorsPalette": [
                    {"hexColor": "#111111"},
                    {"hexColor": "#F8F8F8"},
                    {"hexColor": "#00FF00"},
                    {"hexColor": "#FF0000"},
                    {"hexColor": "#0000FF"},
                    {"hexColor": "#FF00BF"},
                ],
            }
        )
        grouped = {
            17: [1],
            27: [10, 11],
            28: [20],
            29: [30],
            30: [40],
            31: [50],
            18: [60],
            8: [70, 71, 72, 73],
            41: [80, 81],
        }
        first = freaky_avatar(grouped, rules, random.Random(1))
        second = freaky_avatar(grouped, rules, random.Random(2))
        self.assertNotEqual(first["body"], second["body"])
        for look in (first, second):
            model = look["body"]["avatarDefinition"]["updateAvatarModel"]
            if model["playerAvatarType"] == 3:
                scales = model["scales"]
                self.assertIn(scales["height"], (0.9, 1.05))
                self.assertIn(scales["width"], (0.7, 1.0))
                self.assertIn(scales["proportion"], (0.0, 1.0))
                self.assertIn(scales["bodyType"], (0.0, 1.0))
                self.assertNotEqual(scales["proportion"], scales["bodyType"])
            else:
                self.assertEqual(model["playerAvatarType"], 1)
                self.assertNotIn("scales", model)
                self.assertNotIn(79, [item["id"] for item in model["assets"]])
            colors = list(look["colors"].values())
            self.assertEqual(len(colors), 6)
            self.assertEqual(len(set(colors)), 6)
            ids = [item["id"] for item in look["body"]["avatarDefinition"]["updateAvatarModel"]["assets"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertLessEqual(sum(1 for asset_id in ids if asset_id in {70, 71, 72, 73}), 3)
            self.assertLessEqual(sum(1 for asset_id in ids if asset_id in {80, 81}), 1)
            self.assertTrue(any(asset_id in {10, 11} for asset_id in ids))
            hats = [item for item in look["body"]["avatarDefinition"]["updateAvatarModel"]["assets"] if item["id"] in {70, 71, 72, 73}]
            self.assertTrue(hats)
            for hat in hats:
                self.assertIn(hat["meta"]["position"]["X"], (-0.25, 0.25))
                self.assertIn(hat["meta"]["rotation"]["Y"], (-30.0, 30.0))
                self.assertIn(hat["meta"]["scale"]["Z"], (0.8, 1.2))
            self.assertEqual(
                look["fallback"]["avatarDefinition"]["updateAvatarModel"]["assets"],
                look["body"]["avatarDefinition"]["updateAvatarModel"]["assets"],
            )

    def test_saved_outfit_clothes_stay_off(self) -> None:
        import random

        rules = parse_avatar_rules(
            {
                "scales": {"height": {"min": 0.9, "max": 1.05, "increment": 0.01}},
                "wearableAssetTypes": [
                    {"id": 8, "maxNumber": 3},
                    {"id": 11, "maxNumber": 1},
                    {"id": 12, "maxNumber": 1},
                ],
                "bodyColorsPalette": [{"hexColor": "#111111"}, {"hexColor": "#00FF00"}, {"hexColor": "#FF0000"}, {"hexColor": "#0000FF"}, {"hexColor": "#FF00BF"}, {"hexColor": "#FFFF00"}],
            }
        )
        grouped = {
            17: [1],
            27: [10],
            28: [20],
            29: [30],
            30: [40],
            31: [50],
            18: [60],
            8: [70, 71],
            11: [900, 901],
            12: [910, 911],
        }
        blocked = {70, 900, 910}
        for seed in range(8):
            look = freaky_avatar(grouped, rules, random.Random(seed), blocked)
            ids = [item["id"] for item in look["body"]["avatarDefinition"]["updateAvatarModel"]["assets"]]
            self.assertNotIn(70, ids)
            self.assertNotIn(900, ids)
            self.assertNotIn(910, ids)
            self.assertIn(10, ids)

    def test_some_looks_are_r6(self) -> None:
        import random

        rules = parse_avatar_rules(
            {
                "scales": {"height": {"min": 0.9, "max": 1.05, "increment": 0.01}},
                "wearableAssetTypes": [{"id": 8, "maxNumber": 3}],
                "bodyColorsPalette": [{"hexColor": "#111111"}, {"hexColor": "#00FF00"}, {"hexColor": "#FF0000"}, {"hexColor": "#0000FF"}, {"hexColor": "#FF00BF"}, {"hexColor": "#FFFF00"}],
            }
        )
        grouped = {17: [1], 79: [2], 27: [10], 28: [20], 29: [30], 30: [40], 31: [50], 8: [70], 64: [500]}
        types = {
            freaky_avatar(grouped, rules, random.Random(seed))["body"]["avatarDefinition"]["updateAvatarModel"]["playerAvatarType"]
            for seed in range(40)
        }
        self.assertEqual(types, {1, 3})


if __name__ == "__main__":
    unittest.main()
