"""Instagram Graph Business Discovery audit. HTTP is mocked."""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/ig-graph-audit-test.db")

import main
from fastapi.testclient import TestClient


TOKEN = "graph-token-test"
ACCOUNT_ID = "17841405309211844"

DISCOVERY = {
    "id": "17841401441775531",
    "username": "bluebottle",
    "name": "Blue Bottle Coffee",
    "biography": "Coffee",
    "website": "https://bluebottlecoffee.com",
    "followers_count": 100,
    "follows_count": 20,
    "media_count": 2,
    "profile_picture_url": "https://scontent.cdninstagram.com/blue.jpg",
    "media": {
        "data": [
            {
                "id": "17858843269216389",
                "caption": "Morning pour",
                "like_count": 10,
                "comments_count": 5,
                "media_type": "IMAGE",
                "timestamp": "2026-07-01T12:00:00+0000",
                "permalink": "https://www.instagram.com/p/Abc123/",
                "shortcode": "Abc123",
            },
            {
                "id": "17894036119131554",
                "caption": "Reel",
                "comments_count": 1,
                "media_type": "VIDEO",
                "timestamp": "2026-07-02T12:00:00+0000",
                "permalink": "https://www.instagram.com/reel/Def456/",
                "view_count": 80,
            },
        ]
    },
}


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def graph_error(status, code, message, subcode=None, title=None, user_msg=None):
    error = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    if title:
        error["error_user_title"] = title
    if user_msg:
        error["error_user_msg"] = user_msg
    return FakeResponse(status, {"error": error})


class GraphAuditTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self._env = {
            name: os.environ.get(name)
            for name in (
                "IG_GRAPH_ACCESS_TOKEN",
                "IG_BUSINESS_ACCOUNT_ID",
                "IG_GRAPH_API_VERSION",
                "GEMINI_API_KEY",
            )
        }
        os.environ["IG_GRAPH_ACCESS_TOKEN"] = TOKEN
        os.environ["IG_BUSINESS_ACCOUNT_ID"] = ACCOUNT_ID
        os.environ.pop("IG_GRAPH_API_VERSION", None)
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()
        self.previous_rapid = main.RAPIDAPI_KEY

    def tearDown(self):
        main.RAPIDAPI_KEY = self.previous_rapid
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _patch_pipeline(self, responses, audit_model="gemini-3.6-flash"):
        calls = []

        async def fake_get(url, params=None, timeout=None, follow_redirects=None, headers=None):
            calls.append({
                "url": url,
                "params": params,
                "follow_redirects": follow_redirects,
            })
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        saved = {}

        async def fake_save(url, username):
            saved["url"] = url
            saved["username"] = username
            if not url:
                return None
            return "https://antimprompt.com/uploads/bluebottle_profile.jpg"

        seen = {}

        def fake_audit(profile, posts, engagement, gender):
            seen["profile"] = profile
            seen["posts"] = posts
            seen["engagement"] = engagement
            seen["gender"] = gender
            report = main.AuditReport.model_validate({
                "overall_score": 70,
                "profile": {"score": 70, "reason": "ok"},
                "content": {"score": 70, "reason": "ok"},
                "engagement": {"score": 70, "reason": "ok"},
                "consistency": {"score": 70, "reason": "ok"},
                "growth_potential": {"score": 70, "reason": "ok"},
                "strengths": ["clear bio"],
                "weaknesses": ["few posts"],
                "recommendations": ["post consistently"],
                "content_strategy": ["reels"],
                "hashtag_strategy": ["niche tags"],
                "audience_gender": {"available": False, "source": "not_available"},
                "summary": "Solid start.",
            })
            return report, audit_model

        stack = patch.multiple(
            main.http_client,
            get=fake_get,
        )
        return calls, saved, seen, stack, patch.object(
            main, "save_profile_pic_to_hostinger", fake_save
        ), patch.object(main, "generate_ai_audit", fake_audit)

    def test_fields_use_documented_names_and_default_version(self):
        fields = main.business_discovery_fields("bluebottle")
        self.assertIn("business_discovery.username(bluebottle)", fields)
        for name in (
            "username",
            "name",
            "biography",
            "followers_count",
            "follows_count",
            "media_count",
            "profile_picture_url",
            "caption",
            "like_count",
            "comments_count",
            "media_type",
            "timestamp",
            "permalink",
        ):
            self.assertIn(name, fields)
        self.assertEqual(main.ig_graph_api_version(), "v26.0")
        os.environ["IG_GRAPH_API_VERSION"] = "26.0"
        self.assertEqual(main.ig_graph_api_version(), "v26.0")

    def test_missing_config_is_500_and_does_not_call_graph(self):
        os.environ.pop("IG_GRAPH_ACCESS_TOKEN")
        os.environ.pop("IG_BUSINESS_ACCOUNT_ID")
        with patch.object(main.http_client, "get", AsyncMock()) as get:
            response = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
        self.assertEqual(response.status_code, 500)
        self.assertIn("IG_GRAPH_ACCESS_TOKEN", response.json()["detail"])
        get.assert_not_called()

    def test_success_maps_into_existing_audit_shape(self):
        calls, saved, seen, http_patch, save_patch, audit_patch = self._patch_pipeline([
            FakeResponse(200, {"business_discovery": DISCOVERY, "id": ACCOUNT_ID}),
        ])
        with http_patch, save_patch, audit_patch:
            response = self.client.get("/ai-magic/insta-audit-v2/@BlueBottle")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["username"], "bluebottle")
        self.assertEqual(body["source"], "instagram_graph")
        self.assertEqual(body["ai_audit_model"], "gemini-3.6-flash")
        self.assertEqual(body["profile"]["full_name"], "Blue Bottle Coffee")
        self.assertEqual(body["profile"]["followers"], 100)
        self.assertEqual(body["profile"]["following"], 20)
        self.assertEqual(body["profile"]["total_posts"], 2)
        self.assertEqual(
            body["profile"]["profile_pic_url"],
            "https://antimprompt.com/uploads/bluebottle_profile.jpg",
        )
        self.assertFalse(body["profile"]["is_private"])
        self.assertEqual(saved["url"], DISCOVERY["profile_picture_url"])
        self.assertEqual(len(body["recent_posts"]), 2)
        self.assertEqual(body["recent_posts"][0]["likes"], 10)
        self.assertEqual(body["recent_posts"][0]["comments"], 5)
        self.assertEqual(body["recent_posts"][0]["type"], "IMAGE")
        self.assertEqual(body["recent_posts"][1]["shortcode"], "Def456")
        self.assertEqual(body["recent_posts"][1]["video_views"], 80)
        self.assertEqual(body["recent_posts"][1]["likes"], 0)
        self.assertTrue(body["recent_posts"][1]["is_video"])
        self.assertFalse(body["follower_gender"]["available"])
        self.assertEqual(body["follower_gender"]["source"], "not_available")
        self.assertEqual(body["engagement"]["average_likes"], 5.0)
        self.assertEqual(calls[0]["url"], f"https://graph.facebook.com/v26.0/{ACCOUNT_ID}")
        self.assertEqual(calls[0]["params"]["access_token"], TOKEN)
        self.assertFalse(calls[0]["follow_redirects"])
        self.assertIn("business_discovery.username(bluebottle)", calls[0]["params"]["fields"])
        self.assertFalse(seen["gender"]["available"])
        self.assertEqual(seen["posts"][0]["caption"], "Morning pour")

    def test_second_request_uses_cache_and_shares_semaphore_key_space(self):
        calls, _, _, http_patch, save_patch, audit_patch = self._patch_pipeline([
            FakeResponse(200, {"business_discovery": DISCOVERY, "id": ACCOUNT_ID}),
        ])
        with http_patch, save_patch, audit_patch:
            first = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
            second = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertIn("graph:bluebottle", main.AUDIT_CACHE)
        self.assertNotIn("bluebottle", main.AUDIT_CACHE)

    def test_personal_or_missing_account_is_404(self):
        calls, _, _, http_patch, save_patch, audit_patch = self._patch_pipeline([
            graph_error(
                400,
                110,
                "Invalid user id",
                subcode=2207013,
                title="Cannot find User",
                user_msg="The user with username: personal.account cannot be found.",
            ),
        ])
        with http_patch, save_patch, audit_patch:
            response = self.client.get("/ai-magic/insta-audit-v2/personal.account")
        self.assertEqual(response.status_code, 404)
        detail = response.json()["detail"]
        self.assertIn("Personal accounts", detail)
        self.assertIn("@personal.account", detail)
        self.assertEqual(len(calls), 1)

    def test_expired_token_is_401_and_rate_limit_is_429(self):
        cases = [
            (graph_error(400, 190, "Error validating access token: session has expired"), 401, "IG_GRAPH_ACCESS_TOKEN"),
            (graph_error(400, 80002, "(#80002) too many calls"), 429, "rate limit"),
            (FakeResponse(429, {"error": {"message": "slow down", "code": 4}}), 429, "rate limit"),
        ]
        for payload, status, needle in cases:
            main.AUDIT_CACHE.clear()
            calls, _, _, http_patch, save_patch, audit_patch = self._patch_pipeline([payload])
            with http_patch, save_patch, audit_patch:
                response = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
            self.assertEqual(response.status_code, status, response.text)
            self.assertIn(needle, response.json()["detail"])
            self.assertNotIn(TOKEN, response.json()["detail"])

    def test_error_detail_redacts_token(self):
        calls, _, _, http_patch, save_patch, audit_patch = self._patch_pipeline([
            graph_error(400, 1, f"temporary failure {TOKEN}"),
        ])
        with http_patch, save_patch, audit_patch:
            response = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(TOKEN, response.text)
        self.assertIn("[redacted]", response.json()["detail"])
        self.assertTrue(calls)

    def test_nonpublic_field_retries_with_public_fields_only(self):
        calls, _, seen, http_patch, save_patch, audit_patch = self._patch_pipeline([
            graph_error(400, 100, "(#100) Tried accessing nonexisting field (profile_picture_url)"),
            FakeResponse(200, {
                "business_discovery": {
                    "id": "17841401441775531",
                    "username": "bluebottle",
                    "biography": "Coffee",
                    "followers_count": 100,
                    "media_count": 0,
                },
                "id": ACCOUNT_ID,
            }),
        ])
        with http_patch, save_patch, audit_patch:
            response = self.client.get("/ai-magic/insta-audit-v2/bluebottle")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("profile_picture_url", calls[1]["params"]["fields"])
        self.assertNotIn("follows_count", calls[1]["params"]["fields"])
        self.assertIn("followers_count", calls[1]["params"]["fields"])
        self.assertIsNone(seen["profile"]["profile_pic_url"])
        self.assertEqual(seen["profile"]["followers"], 100)

    def test_invalid_username_is_rejected_before_the_request(self):
        with patch.object(main.http_client, "get", AsyncMock()) as get:
            response = self.client.get("/ai-magic/insta-audit-v2/bad name")
        self.assertEqual(response.status_code, 400)
        get.assert_not_called()

    def test_rapidapi_route_still_reports_its_own_config_error(self):
        main.RAPIDAPI_KEY = None
        with patch.object(main.http_client, "get", AsyncMock()) as get:
            response = self.client.get("/ai-magic/insta-audit/bluebottle")
        self.assertEqual(response.status_code, 500)
        self.assertIn("RAPIDAPI_KEY", response.json()["detail"])
        get.assert_not_called()

    def test_cache_keys_do_not_mix_sources(self):
        async def run():
            async def rapid(name):
                return {"status": "success", "source": "rapid", "username": name}

            async def graph(name):
                return {"status": "success", "source": "graph", "username": name}

            main.AUDIT_CACHE.clear()
            first = await main.get_cached_or_run_audit("bluebottle", runner=rapid)
            second = await main.get_cached_or_run_audit(
                "bluebottle",
                runner=graph,
                cache_key="graph:bluebottle",
            )
            cached_rapid = await main.get_cached_or_run_audit("bluebottle", runner=graph)
            return first, second, cached_rapid

        import asyncio
        first, second, cached_rapid = asyncio.run(run())
        self.assertEqual(first["source"], "rapid")
        self.assertEqual(second["source"], "graph")
        self.assertEqual(cached_rapid["source"], "rapid")


if __name__ == "__main__":
    unittest.main()
