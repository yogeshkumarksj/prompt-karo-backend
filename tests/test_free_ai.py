"""Free AI features, budget cap, and Instagram audit bug fixes. No live provider calls."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/free-ai-test.db")

import ai_gateway
import main
from fastapi import HTTPException
from fastapi.testclient import TestClient


ENV_KEYS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "AI_PROVIDER_ORDER",
    "FREE_GEMINI_MODELS",
    "GROQ_MODELS",
    "GEMINI_MODELS",
    "AI_GLOBAL_DAILY_REQUEST_CAP",
    "AI_GLOBAL_MONTHLY_REQUEST_CAP",
    "AI_GLOBAL_DAILY_TOKEN_CAP",
    "AI_GLOBAL_MONTHLY_TOKEN_CAP",
    "AI_FREE_USER_DAILY_LIMIT",
    "AI_PAID_USER_DAILY_LIMIT",
    "AI_BLOCK_PAID_FEATURES",
    "AI_BUDGET_ENABLED",
    "AI_FEATURE_BILLING",
    "AI_RESERVE_TOKENS_PER_CALL",
    "AI_CACHE_TTL_SECONDS",
)

SONG_REPORT = {
    "songs": [
        {
            "title": "Test Track",
            "artist": "Test Artist",
            "why_trending": "The beat fits short cuts.",
            "reel_idea": "Show one step on each beat.",
            "hook": "Start on the first kick.",
            "hashtags": ["fitness", "reels"],
        }
    ],
    "how_to_use": ["Save the audio and reuse it on three reels this week."],
    "summary": "One reusable audio idea.",
}

ART_REPORT = {
    "trends": [
        {
            "name": "Glass studio",
            "why_it_works": "Bright reflections read clearly on a phone screen.",
            "visual_cues": ["soft window light", "one product"],
            "prompt": "A single ceramic cup on a sunlit table, soft shadows, square photo.",
            "negative_prompt": "blurry, extra fingers, watermark",
            "caption_idea": "Morning light, one cup.",
        }
    ],
    "workflow": ["Generate four variations and keep the clearest one."],
    "summary": "One bright product style.",
}


class FakeHttp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _completion(payload, tokens=90):
    return FakeHttp(
        200,
        {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"total_tokens": tokens},
        },
    )


class FreeAiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self._env = {name: os.environ.get(name) for name in ENV_KEYS}
        self._rapid = main.RAPIDAPI_KEY
        self._gemini = main.GEMINI_API_KEY
        self._now = ai_gateway._now
        for name in ENV_KEYS:
            os.environ.pop(name, None)
        ai_gateway.reset_state_for_tests()
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()

    def tearDown(self):
        ai_gateway._now = self._now
        main.RAPIDAPI_KEY = self._rapid
        main.GEMINI_API_KEY = self._gemini
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        ai_gateway.reset_state_for_tests()
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()

    def test_billing_defaults_and_override(self):
        self.assertEqual(ai_gateway.feature_billing("viral-insta-songs"), "free")
        self.assertEqual(ai_gateway.feature_billing("viral-ai-art-trends"), "free")
        self.assertEqual(ai_gateway.feature_billing("insta-audit"), "paid")
        self.assertEqual(ai_gateway.feature_billing("insta-audit-v2"), "paid")
        self.assertEqual(ai_gateway.feature_billing("insta-audit-auto"), "paid")
        os.environ["AI_FEATURE_BILLING"] = "viral-insta-songs:paid, insta-audit:free"
        self.assertEqual(ai_gateway.feature_billing("viral-insta-songs"), "paid")
        self.assertEqual(ai_gateway.feature_billing("insta-audit"), "free")
        self.assertEqual(ai_gateway.feature_billing("viral-ai-art-trends"), "free")

    def test_unsafe_model_names_are_dropped(self):
        os.environ["FREE_GEMINI_MODELS"] = "gemini-3.6-flash, ../../etc/passwd, gemini-3.5-flash-lite"
        self.assertEqual(
            ai_gateway.gemini_models(),
            ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
        )

    def test_paid_flag_blocks_before_any_call(self):
        os.environ["AI_BLOCK_PAID_FEATURES"] = "true"
        with self.assertRaises(HTTPException) as caught:
            ai_gateway.begin_generation("insta-audit", "203.0.113.8")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("paid", caught.exception.detail)
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.8")
        self.assertTrue(any(key.startswith("req:day:") for key in ai_gateway.usage_snapshot()))

    def test_global_cap_and_daily_rollover(self):
        os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "1"
        os.environ["AI_GLOBAL_MONTHLY_REQUEST_CAP"] = "10"
        ai_gateway._now = lambda: datetime(2026, 11, 15, tzinfo=timezone.utc)
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.9")
        with self.assertRaises(HTTPException) as caught:
            ai_gateway.begin_generation("viral-ai-art-trends", "203.0.113.10")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.detail, "Limit reached, try tomorrow.")
        ai_gateway._now = lambda: datetime(2026, 11, 16, tzinfo=timezone.utc)
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.9")
        snap = ai_gateway.usage_snapshot()
        self.assertEqual(snap["req:day:2026-11-16"], 1)
        self.assertEqual(snap["req:month:2026-11"], 2)

    def test_monthly_cap_survives_a_new_day(self):
        os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "10"
        os.environ["AI_GLOBAL_MONTHLY_REQUEST_CAP"] = "1"
        ai_gateway._now = lambda: datetime(2026, 11, 15, tzinfo=timezone.utc)
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.11")
        ai_gateway._now = lambda: datetime(2026, 11, 16, tzinfo=timezone.utc)
        with self.assertRaises(HTTPException) as caught:
            ai_gateway.begin_generation("viral-insta-songs", "203.0.113.11")
        self.assertEqual(caught.exception.detail, "Limit reached, try tomorrow.")

    def test_user_limit_is_per_person_and_per_feature(self):
        os.environ["AI_FREE_USER_DAILY_LIMIT"] = "1"
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.12")
        with self.assertRaises(HTTPException):
            ai_gateway.begin_generation("viral-insta-songs", "203.0.113.12")
        ai_gateway.begin_generation("viral-insta-songs", "203.0.113.13")
        ai_gateway.begin_generation("viral-ai-art-trends", "203.0.113.12")

    def test_token_settle_replaces_the_reserve(self):
        os.environ["AI_RESERVE_TOKENS_PER_CALL"] = "4000"
        ai_gateway.begin_generation("viral-insta-songs", "")
        day_key = next(key for key in ai_gateway.usage_snapshot() if key.startswith("tok:day:"))
        self.assertEqual(ai_gateway.usage_snapshot()[day_key], 4000)
        ai_gateway.settle_tokens(0)
        self.assertEqual(ai_gateway.usage_snapshot()[day_key], 4000)
        ai_gateway.settle_tokens(90)
        self.assertEqual(ai_gateway.usage_snapshot()[day_key], 90)

    def test_cap_zero_does_not_call_a_provider(self):
        os.environ["GROQ_API_KEY"] = "groq-test-key"
        os.environ["AI_PROVIDER_ORDER"] = "groq"
        os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "0"
        calls = []

        async def fake_post(url, *, headers, body, timeout):
            calls.append(url)
            return _completion(SONG_REPORT)

        with patch.object(ai_gateway, "_post", fake_post):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(
                    ai_gateway.viral_insta_songs("fitness", "India", "Hindi", "203.0.113.20")
                )
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.detail, "Limit reached, try tomorrow.")
        self.assertEqual(calls, [])

    def test_gemini_falls_back_to_groq_and_redacts_keys(self):
        os.environ["GEMINI_API_KEY"] = "gemini-secret-value"
        os.environ["GROQ_API_KEY"] = "groq-secret-value"
        os.environ["AI_PROVIDER_ORDER"] = "gemini,groq"
        calls = []

        async def fake_post(url, *, headers, body, timeout):
            calls.append(url)
            self.assertNotIn("gemini-secret-value", url)
            self.assertNotIn("groq-secret-value", url)
            if "generativelanguage.googleapis.com" in url:
                return FakeHttp(429, {"error": {"message": "exhausted gemini-secret-value"}})
            return _completion(SONG_REPORT, tokens=120)

        with patch.object(ai_gateway, "_post", fake_post):
            parsed, provider, model, tokens = asyncio.run(
                ai_gateway.complete_json("give me json", ai_gateway.SongsReport)
            )
        self.assertEqual(provider, "groq")
        self.assertEqual(tokens, 120)
        self.assertEqual(parsed.songs[0].title, "Test Track")
        self.assertEqual(len(calls), 2)
        self.assertIn("generativelanguage.googleapis.com", calls[0])
        self.assertIn("api.groq.com", calls[1])

        async def failing_post(url, *, headers, body, timeout):
            return FakeHttp(503, {"error": {"message": "down groq-secret-value"}})

        with patch.object(ai_gateway, "_post", failing_post):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(ai_gateway.complete_json("give me json", ai_gateway.SongsReport))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("groq-secret-value", caught.exception.detail)
        self.assertNotIn("gemini-secret-value", caught.exception.detail)
        self.assertIn("[redacted]", caught.exception.detail)

    def test_gemini_retries_without_json_mode_after_a_400(self):
        os.environ["GEMINI_API_KEY"] = "gemini-secret-value"
        os.environ["AI_PROVIDER_ORDER"] = "gemini"
        modes = []

        async def fake_post(url, *, headers, body, timeout):
            config = body.get("generationConfig") or {}
            modes.append(config.get("responseMimeType"))
            if len(modes) == 1:
                return FakeHttp(400, {"error": {"message": "responseMimeType unsupported"}})
            return FakeHttp(
                200,
                {
                    "candidates": [{"content": {"parts": [{"text": json.dumps(SONG_REPORT)}]}}],
                    "usageMetadata": {"totalTokenCount": 40},
                },
            )

        with patch.object(ai_gateway, "_post", fake_post):
            parsed, provider, _model, tokens = asyncio.run(
                ai_gateway.complete_json("give me json", ai_gateway.SongsReport)
            )
        self.assertEqual(provider, "gemini")
        self.assertEqual(tokens, 40)
        self.assertEqual(parsed.summary, "One reusable audio idea.")
        self.assertEqual(modes, ["application/json", None])

    def _install_provider(self, calls):
        os.environ["GROQ_API_KEY"] = "groq-test-key"
        os.environ["AI_PROVIDER_ORDER"] = "groq"

        async def fake_post(url, *, headers, body, timeout):
            calls.append(url)
            prompt = body["messages"][-1]["content"]
            payload = ART_REPORT if "visual_cues" in prompt else SONG_REPORT
            return _completion(payload, tokens=80)

        return patch.object(ai_gateway, "_post", fake_post)

    def test_song_and_art_routes_cache_and_keep_the_free_flag(self):
        calls = []
        with self._install_provider(calls):
            songs = self.client.post(
                "/ai-magic/viral-insta-songs",
                json={"niche": " fitness ", "region": "India", "language": "Hindi"},
            )
            again = self.client.post(
                "/ai-magic/viral-insta-songs",
                json={"niche": "Fitness", "region": "india", "language": "hindi"},
            )
            art = self.client.post(
                "/ai-magic/viral-ai-art-trends",
                json={"niche": "ceramics", "platform": "Instagram"},
            )
        self.assertEqual(songs.status_code, 200, songs.text)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(art.status_code, 200, art.text)
        self.assertEqual(songs.json()["feature"], "Viral Insta Songs")
        self.assertEqual(songs.json()["billing"], "free")
        self.assertFalse(songs.json()["cached"])
        self.assertEqual(songs.json()["songs"][0]["title"], "Test Track")
        self.assertIn("not a live Instagram scan", songs.json()["disclaimer"])
        self.assertTrue(again.json()["cached"])
        self.assertEqual(art.json()["feature"], "Viral AI Art Trends")
        self.assertEqual(art.json()["billing"], "free")
        self.assertEqual(art.json()["trends"][0]["name"], "Glass studio")
        self.assertEqual(len(calls), 2)
        user_buckets = [key for key in ai_gateway.usage_snapshot() if key.startswith("user:")]
        self.assertEqual(sum(ai_gateway.usage_snapshot()[key] for key in user_buckets), 2)

    def test_song_route_user_limit_still_serves_the_cached_input(self):
        os.environ["AI_FREE_USER_DAILY_LIMIT"] = "1"
        calls = []
        with self._install_provider(calls):
            first = self.client.post("/ai-magic/viral-insta-songs", json={"niche": "fitness"})
            blocked = self.client.post("/ai-magic/viral-insta-songs", json={"niche": "food"})
            cached = self.client.post("/ai-magic/viral-insta-songs", json={"niche": "fitness"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["detail"], "Limit reached, try tomorrow.")
        self.assertEqual(cached.status_code, 200, cached.text)
        self.assertTrue(cached.json()["cached"])
        self.assertEqual(len(calls), 1)

    def test_audit_route_stops_before_rapidapi_when_the_budget_is_spent(self):
        main.RAPIDAPI_KEY = "rapid-test-key"
        main.GEMINI_API_KEY = "gemini-test-key"
        os.environ["GEMINI_API_KEY"] = "gemini-test-key"
        os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "0"
        with patch.object(main.http_client, "get", AsyncMock()) as get:
            response = self.client.get("/ai-magic/insta-audit/bluebottle")
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["detail"], "Limit reached, try tomorrow.")
        get.assert_not_called()

    def test_audit_route_counts_each_caller_toward_the_daily_limit(self):
        main.RAPIDAPI_KEY = "rapid-test-key"
        main.GEMINI_API_KEY = "gemini-test-key"
        os.environ["GEMINI_API_KEY"] = "gemini-test-key"
        os.environ["AI_PAID_USER_DAILY_LIMIT"] = "1"
        os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "10"
        calls = []

        class PrivateProfile:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "username": "bluebottle",
                    "is_private": True,
                    "edge_followed_by": {"count": 1},
                    "edge_follow": {"count": 1},
                    "edge_owner_to_timeline_media": {"count": 0, "edges": []},
                }

        async def fake_get(*_args, **_kwargs):
            calls.append("profile")
            return PrivateProfile()

        with patch.object(main.http_client, "get", fake_get):
            first = self.client.get("/ai-magic/insta-audit/bluebottle")
            second = self.client.get("/ai-magic/insta-audit/othername")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "private")
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["detail"], "Limit reached, try tomorrow.")
        self.assertEqual(calls, ["profile"])

    def test_invalid_username_and_provider_body_are_not_forwarded(self):
        with patch.object(main.http_client, "get", AsyncMock()) as get:
            response = self.client.get("/ai-magic/insta-audit/bad name")
        self.assertEqual(response.status_code, 400)
        get.assert_not_called()

        main.RAPIDAPI_KEY = "rapid-secret"
        main.GEMINI_API_KEY = "gemini-secret"

        class BodyResponse:
            status_code = 503
            text = "SECRET-BODY-MARKER rapid-secret"

            def json(self):
                return {"status": False}

        async def fake_get(*_args, **_kwargs):
            return BodyResponse()

        with patch.object(main.http_client, "get", fake_get):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("SECRET-BODY-MARKER", caught.exception.detail)
        self.assertNotIn("rapid-secret", caught.exception.detail)
        self.assertIn("503", caught.exception.detail)

    def test_profile_metrics_survive_nulls_wrappers_and_string_booleans(self):
        metrics = main.extract_profile_metrics(
            {
                "username": "tester",
                "edge_followed_by": None,
                "edge_follow": None,
                "edge_owner_to_timeline_media": None,
                "is_private": "false",
                "follower_count": 15,
                "following_count": 4,
                "media_count": 2,
            }
        )
        self.assertEqual(metrics["followers"], 15)
        self.assertEqual(metrics["following"], 4)
        self.assertEqual(metrics["total_posts"], 2)
        self.assertFalse(metrics["is_private"])

        wrapped = main.unwrap_profile_payload(
            {
                "status": True,
                "graphql": {
                    "user": {
                        "username": "nested",
                        "edge_followed_by": {"count": 3},
                    }
                },
            }
        )
        self.assertEqual(main.extract_profile_metrics(wrapped)["followers"], 3)
        self.assertEqual(
            main.extract_recent_posts({"edge_owner_to_timeline_media": None}),
            [],
        )
        posts = main.extract_recent_posts(
            {
                "edge_owner_to_timeline_media": {
                    "edges": [
                        {
                            "node": {
                                "shortcode": "Ab",
                                "__typename": "GraphImage",
                                "edge_media_to_caption": {"edges": [{"node": {"text": "hello"}}]},
                                "edge_liked_by": {"count": 4},
                                "edge_media_to_comment": None,
                                "is_video": False,
                            }
                        }
                    ]
                }
            }
        )
        self.assertEqual(posts[0]["caption"], "hello")
        self.assertEqual(posts[0]["likes"], 4)
        self.assertEqual(posts[0]["comments"], 0)
        flat = main.extract_recent_posts(
            {"posts": [{"caption": "flat", "like_count": 6, "comment_count": 1, "shortcode": "Cd"}]}
        )
        self.assertEqual(flat[0]["likes"], 6)
        self.assertEqual(flat[0]["comments"], 1)

    def test_profile_image_host_is_limited_to_instagram_https(self):
        self.assertTrue(
            main.profile_image_url_allowed("https://scontent.cdninstagram.com/v/t51/pic.jpg")
        )
        self.assertTrue(main.profile_image_url_allowed("https://instagram.fdel1-1.fna.fbcdn.net/a.jpg"))
        self.assertFalse(main.profile_image_url_allowed("http://scontent.cdninstagram.com/pic.jpg"))
        self.assertFalse(main.profile_image_url_allowed("https://169.254.169.254/latest/meta-data"))
        self.assertFalse(main.profile_image_url_allowed("https://evil.com/pic.jpg"))
        self.assertFalse(main.profile_image_url_allowed("https://cdninstagram.com.evil.com/pic.jpg"))

        async def check():
            with patch.object(main.http_client, "get", AsyncMock()) as get:
                result = await main.save_profile_pic_to_hostinger(
                    "https://169.254.169.254/latest/meta-data",
                    "user",
                )
            get.assert_not_called()
            return result

        self.assertIsNone(asyncio.run(check()))


if __name__ == "__main__":
    unittest.main()
