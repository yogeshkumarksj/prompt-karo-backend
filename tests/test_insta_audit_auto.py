"""Auto Instagram audit: Graph first, then RapidAPI, with multi-key fallback.

HTTP is mocked. No live RapidAPI, Graph, or Gemini calls.
"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/insta-audit-auto-test.db")

import ai_gateway
import main
from fastapi import HTTPException
from fastapi.testclient import TestClient


TOKEN = "graph-token-auto-test"
ACCOUNT_ID = "17841405309211844"

DISCOVERY = {
    "id": "17841401441775531",
    "username": "bluebottle",
    "name": "Blue Bottle Coffee",
    "biography": "Coffee",
    "followers_count": 100,
    "follows_count": 20,
    "media_count": 1,
    "profile_picture_url": "https://scontent.cdninstagram.com/blue.jpg",
    "media": {"data": []},
}

RAPID_PROFILE = {
    "username": "bluebottle",
    "full_name": "Blue Bottle",
    "biography": "Coffee",
    "is_private": False,
    "edge_followed_by": {"count": 10},
    "edge_follow": {"count": 1},
    "edge_owner_to_timeline_media": {"count": 0, "edges": []},
}

ENV_NAMES = (
    "RAPIDAPI_KEY",
    "INSTA_LOOTER_KEY",
    "IG_GRAPH_ACCESS_TOKEN",
    "IG_BUSINESS_ACCOUNT_ID",
    "IG_GRAPH_API_VERSION",
    "GEMINI_API_KEY",
    "RAPIDAPI_KEY_COOLDOWN_SECONDS",
    *[f"RAPIDAPI_KEY_{index}" for index in range(1, 11)],
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def graph_error(status, code, message, subcode=None):
    error = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return FakeResponse(status, {"error": error})


class AutoAuditTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self._env = {name: os.environ.get(name) for name in ENV_NAMES}
        self._rapid = main.RAPIDAPI_KEY
        self._gemini = main.GEMINI_API_KEY
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        os.environ["GEMINI_API_KEY"] = "gemini-auto-test"
        main.GEMINI_API_KEY = "gemini-auto-test"
        main.RAPIDAPI_KEY = None
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()
        main.clear_rapidapi_key_cooldowns()
        ai_gateway.reset_state_for_tests()

    def tearDown(self):
        main.RAPIDAPI_KEY = self._rapid
        main.GEMINI_API_KEY = self._gemini
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        main.AUDIT_CACHE.clear()
        main.AUDIT_INFLIGHT.clear()
        main.clear_rapidapi_key_cooldowns()
        ai_gateway.reset_state_for_tests()

    def _enable_graph(self):
        os.environ["IG_GRAPH_ACCESS_TOKEN"] = TOKEN
        os.environ["IG_BUSINESS_ACCOUNT_ID"] = ACCOUNT_ID

    def _report(self):
        return main.AuditReport.model_validate({
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

    def _patch_audit(self, fake_get, gemini_error=None):
        gemini_calls = []

        def fake_audit(profile, posts, engagement, gender):
            gemini_calls.append(profile)
            if gemini_error is not None:
                raise gemini_error
            return self._report(), "gemini-3.6-flash"

        async def fake_save(url, username):
            return None

        stack = patch.object(main.http_client, "get", fake_get)
        audit = patch.object(main, "generate_ai_audit", fake_audit)
        save = patch.object(main, "save_profile_pic_to_hostinger", fake_save)
        return gemini_calls, stack, audit, save

    def assert_single_auto_reservation(self):
        snap = ai_gateway.usage_snapshot()
        requests = sum(amount for key, amount in snap.items() if key.startswith("req:day:"))
        tokens = sum(amount for key, amount in snap.items() if key.startswith("tok:day:"))
        users = [key for key in snap if key.startswith("user:")]
        self.assertEqual(requests, 1, snap)
        self.assertEqual(tokens, ai_gateway._reserve_tokens(), snap)
        self.assertEqual(users, [key for key in users if ":insta-audit-auto:" in key], snap)
        self.assertEqual(len(users), 1, snap)

    def test_routes_and_options_stay_available(self):
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertIn("/ai-magic/insta-audit/{username}", paths)
        self.assertIn("/ai-magic/insta-audit-v2/{username}", paths)
        self.assertIn("/ai-magic/insta-audit-auto/{username}", paths)
        options = self.client.options("/ai-magic/insta-audit-auto/bluebottle")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.json()["status"], "ok")

    def test_invalid_username_is_rejected_before_any_upstream_call(self):
        async def fake_get(*_args, **_kwargs):
            raise AssertionError("upstream should not be called")

        with patch.object(main.http_client, "get", fake_get):
            response = self.client.get("/ai-magic/insta-audit-auto/bad name")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ai_gateway.usage_snapshot(), {})

    def test_graph_success_is_returned_with_source_graph(self):
        self._enable_graph()
        calls = []

        async def fake_get(url, params=None, timeout=None, follow_redirects=None, headers=None):
            calls.append({"url": url, "params": params, "headers": headers})
            if "graph.facebook.com" not in str(url):
                raise AssertionError(f"RapidAPI should not be called, got {url}")
            return FakeResponse(200, {"business_discovery": DISCOVERY, "id": ACCOUNT_ID})

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(fake_get)
        with http_patch, audit_patch, save_patch:
            first = self.client.get("/ai-magic/insta-audit-auto/@BlueBottle")
            second = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["source"], "graph")
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["username"], "bluebottle")
        self.assertEqual(body["profile"]["full_name"], "Blue Bottle Coffee")
        self.assertEqual(body["profile"]["followers"], 100)
        self.assertEqual(body["ai_audit_model"], "gemini-3.6-flash")
        self.assertNotIn(TOKEN, first.text)
        self.assertEqual(len(calls), 1)
        self.assertIn(ACCOUNT_ID, calls[0]["url"])
        self.assertEqual(calls[0]["params"]["access_token"], TOKEN)
        self.assertIsNone(calls[0]["headers"])
        self.assertEqual(len(gemini_calls), 1)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["source"], "graph")
        self.assertEqual(len(gemini_calls), 1)
        self.assertIn("auto:bluebottle", main.AUDIT_CACHE)
        self.assertNotIn("bluebottle", main.AUDIT_CACHE)
        self.assertNotIn("graph:bluebottle", main.AUDIT_CACHE)
        self.assert_single_auto_reservation()

    def test_graph_miss_falls_through_to_rapidapi(self):
        self._enable_graph()
        os.environ["RAPIDAPI_KEY_1"] = "rapid-fallback-key"
        calls = []

        async def fake_get(url, params=None, timeout=None, follow_redirects=None, headers=None):
            calls.append({"url": url, "headers": headers})
            if "graph.facebook.com" in str(url):
                return graph_error(
                    400,
                    110,
                    f"Cannot find User {TOKEN}",
                    subcode=2207013,
                )
            self.assertIn("instagram-looter2", str(url))
            self.assertEqual((headers or {}).get("x-rapidapi-key"), "rapid-fallback-key")
            return FakeResponse(200, RAPID_PROFILE)

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(fake_get)
        with http_patch, audit_patch, save_patch:
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "rapidapi")
        self.assertEqual(body["username"], "bluebottle")
        self.assertEqual(body["profile"]["followers"], 10)
        self.assertNotIn("Personal accounts", response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn("rapid-fallback-key", response.text)
        self.assertEqual(len(calls), 2)
        self.assertIn("graph.facebook.com", calls[0]["url"])
        self.assertIn("instagram-looter2", calls[1]["url"])
        self.assertEqual(len(gemini_calls), 1)
        self.assert_single_auto_reservation()

    def test_graph_not_configured_uses_rapidapi_only(self):
        os.environ["RAPIDAPI_KEY"] = "legacy-rapid-key"
        main.RAPIDAPI_KEY = "legacy-rapid-key"
        calls = []

        async def fake_get(url, headers=None, **_kwargs):
            calls.append(url)
            self.assertNotIn("graph.facebook.com", str(url))
            self.assertEqual((headers or {}).get("x-rapidapi-key"), "legacy-rapid-key")
            return FakeResponse(200, RAPID_PROFILE)

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(fake_get)
        with http_patch, audit_patch, save_patch:
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "rapidapi")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(gemini_calls), 1)
        self.assert_single_auto_reservation()

    def test_gemini_error_after_a_graph_profile_does_not_call_rapidapi(self):
        self._enable_graph()
        os.environ["RAPIDAPI_KEY_1"] = "should-not-be-used"
        calls = []

        async def fake_get(url, **_kwargs):
            calls.append(url)
            if "instagram-looter2" in str(url):
                raise AssertionError("RapidAPI ran after Graph already had a profile")
            return FakeResponse(200, {"business_discovery": DISCOVERY, "id": ACCOUNT_ID})

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(
            fake_get,
            gemini_error=HTTPException(status_code=502, detail="gemini down"),
        )
        with http_patch, audit_patch, save_patch:
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"], "gemini down")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(gemini_calls), 1)
        self.assert_single_auto_reservation()

    def test_key1_quota_falls_through_to_key2(self):
        os.environ["RAPIDAPI_KEY_1"] = "quota-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "quota-key-2"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            key = (headers or {}).get("x-rapidapi-key")
            seen.append(key)
            if key == "quota-key-1":
                return FakeResponse(
                    429,
                    {"message": "You have exceeded the MONTHLY quota for Requests on your current plan"},
                )
            return FakeResponse(200, RAPID_PROFILE)

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(fake_get)
        with http_patch, audit_patch, save_patch:
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "rapidapi")
        self.assertEqual(response.json()["profile"]["username"], "bluebottle")
        self.assertEqual(seen, ["quota-key-1", "quota-key-2"])
        self.assertNotIn("quota-key-1", response.text)
        self.assertNotIn("quota-key-2", response.text)
        self.assertEqual(len(gemini_calls), 1)
        self.assert_single_auto_reservation()

    def test_all_keys_exhausted_returns_the_wait_message(self):
        os.environ["RAPIDAPI_KEY_1"] = "exhausted-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "exhausted-key-2"
        os.environ["INSTA_LOOTER_KEY"] = "exhausted-key-1"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            seen.append((headers or {}).get("x-rapidapi-key"))
            return FakeResponse(429, {"message": "You have exceeded the rate limit per hour"})

        gemini_calls, http_patch, audit_patch, save_patch = self._patch_audit(fake_get)
        with http_patch, audit_patch, save_patch:
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")

        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(
            response.json()["detail"],
            "we are getting too much request, please wait",
        )
        self.assertEqual(seen, ["exhausted-key-1", "exhausted-key-2"])
        self.assertNotIn("exhausted-key-1", response.text)
        self.assertNotIn("exhausted-key-2", response.text)
        self.assertEqual(gemini_calls, [])
        self.assert_single_auto_reservation()

    def test_legacy_route_accepts_a_numbered_key_without_rapidapi_key(self):
        os.environ["RAPIDAPI_KEY_4"] = "numbered-only-key"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            seen.append((headers or {}).get("x-rapidapi-key"))
            return FakeResponse(200, {**RAPID_PROFILE, "is_private": True})

        with patch.object(main.http_client, "get", fake_get):
            response = self.client.get("/ai-magic/insta-audit/bluebottle")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "private")
        self.assertEqual(seen, ["numbered-only-key"])

    def test_quota_bodies_rotate_and_other_errors_do_not(self):
        os.environ["RAPIDAPI_KEY_1"] = "body-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "body-key-2"

        async def quota_then_profile(url, headers=None, **_kwargs):
            key = (headers or {}).get("x-rapidapi-key")
            quota_then_profile.seen.append(key)
            if key == "body-key-1":
                return FakeResponse(
                    403,
                    {"message": "You are not subscribed to this API."},
                )
            return FakeResponse(200, {"username": "bluebottle", "biography": "ok"})

        quota_then_profile.seen = []
        with patch.object(main.http_client, "get", quota_then_profile):
            payload = asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(payload["username"], "bluebottle")
        self.assertEqual(quota_then_profile.seen, ["body-key-1", "body-key-2"])

        main.clear_rapidapi_key_cooldowns()
        seen = []

        async def server_error(url, headers=None, **_kwargs):
            seen.append((headers or {}).get("x-rapidapi-key"))
            return FakeResponse(500, {"message": "upstream blew up body-key-1"})

        with patch.object(main.http_client, "get", server_error):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("500", caught.exception.detail)
        self.assertNotIn("upstream blew up", caught.exception.detail)
        self.assertNotIn("body-key-1", caught.exception.detail)
        self.assertEqual(seen, ["body-key-1"])

    def test_plain_200_quota_body_rotates_but_a_real_profile_does_not(self):
        os.environ["RAPIDAPI_KEY_1"] = "plain-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "plain-key-2"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            key = (headers or {}).get("x-rapidapi-key")
            seen.append(key)
            if key == "plain-key-1":
                return FakeResponse(
                    200,
                    {"message": "You have exceeded the MONTHLY quota for Requests on your current plan"},
                )
            return FakeResponse(
                200,
                {"username": "bluebottle", "biography": "posted about a rate limit last year"},
            )

        with patch.object(main.http_client, "get", fake_get):
            payload = asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(payload["username"], "bluebottle")
        self.assertIn("rate limit", payload["biography"])
        self.assertEqual(seen, ["plain-key-1", "plain-key-2"])

    def test_sync_profile_rotates_quota_keys(self):
        os.environ["RAPIDAPI_KEY_1"] = "sync-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "sync-key-2"
        seen = []

        def fake_get(url, headers=None, params=None, timeout=None):
            key = headers["x-rapidapi-key"]
            seen.append(key)
            if key == "sync-key-1":
                return FakeResponse(429, {"message": "rate limit exceeded"})
            return FakeResponse(200, {"username": "bluebottle", "biography": "ok"})

        with patch.object(main.requests, "get", fake_get):
            payload = main.get_instagram_profile("bluebottle")
        self.assertEqual(payload["username"], "bluebottle")
        self.assertEqual(seen, ["sync-key-1", "sync-key-2"])

    def test_exhausted_key_is_skipped_on_the_next_request(self):
        os.environ["RAPIDAPI_KEY_COOLDOWN_SECONDS"] = "3600"
        os.environ["RAPIDAPI_KEY_1"] = "cool-key-1"
        os.environ["RAPIDAPI_KEY_2"] = "cool-key-2"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            key = (headers or {}).get("x-rapidapi-key")
            seen.append(key)
            if key == "cool-key-1":
                return FakeResponse(429, {"message": "exceeded the daily quota"})
            return FakeResponse(200, {"username": "bluebottle"})

        with patch.object(main.http_client, "get", fake_get):
            first = asyncio.run(main.get_instagram_profile_async("bluebottle"))
            seen.clear()
            second = asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(first["username"], "bluebottle")
        self.assertEqual(second["username"], "bluebottle")
        self.assertEqual(seen, ["cool-key-2"])

        main.clear_rapidapi_key_cooldowns()
        cooled = []

        async def both_limited(url, headers=None, **_kwargs):
            cooled.append((headers or {}).get("x-rapidapi-key"))
            return FakeResponse(403, {"message": "subscription quota exceeded"})

        with patch.object(main.http_client, "get", both_limited):
            with self.assertRaises(HTTPException) as first_error:
                asyncio.run(main.get_instagram_profile_async("bluebottle"))
            cooled.clear()
            with self.assertRaises(HTTPException) as second_error:
                asyncio.run(main.get_instagram_profile_async("bluebottle"))
        self.assertEqual(first_error.exception.status_code, 429)
        self.assertEqual(
            first_error.exception.detail,
            "we are getting too much request, please wait",
        )
        self.assertEqual(second_error.exception.status_code, 429)
        self.assertEqual(
            second_error.exception.detail,
            "we are getting too much request, please wait",
        )
        self.assertEqual(cooled, [])

    def test_transport_error_redacts_every_configured_key_and_does_not_rotate(self):
        secrets = [f"rapid-secret-{index}-zzzz" for index in range(1, 11)]
        for index, secret in enumerate(secrets, start=1):
            os.environ[f"RAPIDAPI_KEY_{index}"] = secret
        os.environ["RAPIDAPI_KEY"] = "rapid-legacy-secret-zzzz"
        os.environ["INSTA_LOOTER_KEY"] = "looter-legacy-secret-zzzz"
        main.RAPIDAPI_KEY = "module-rapid-secret-zzzz"
        seen = []

        async def fake_get(url, headers=None, **_kwargs):
            seen.append((headers or {}).get("x-rapidapi-key"))
            raise httpx.RequestError(
                "connect failed "
                + " ".join(secrets)
                + " rapid-legacy-secret-zzzz looter-legacy-secret-zzzz module-rapid-secret-zzzz"
            )

        with patch.object(main.http_client, "get", fake_get):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.get_instagram_profile_async("bluebottle"))
        detail = caught.exception.detail
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(seen, ["rapid-secret-1-zzzz"])
        self.assertIn("[redacted]", detail)
        for secret in secrets:
            self.assertNotIn(secret, detail)
        self.assertNotIn("rapid-legacy-secret-zzzz", detail)
        self.assertNotIn("looter-legacy-secret-zzzz", detail)
        self.assertNotIn("module-rapid-secret-zzzz", detail)

        redacted = main._redact_audit_error(
            " ".join(secrets)
            + " rapid-legacy-secret-zzzz looter-legacy-secret-zzzz module-rapid-secret-zzzz"
        )
        for secret in secrets:
            self.assertNotIn(secret, redacted)
        self.assertNotIn("module-rapid-secret-zzzz", redacted)

    def test_key_order_is_numbered_then_legacy_and_deduped(self):
        os.environ["RAPIDAPI_KEY_2"] = "k-two"
        os.environ["RAPIDAPI_KEY_1"] = "k-one"
        os.environ["RAPIDAPI_KEY_3"] = "k-one"
        os.environ["RAPIDAPI_KEY"] = "k-env"
        os.environ["INSTA_LOOTER_KEY"] = "k-looter"
        main.RAPIDAPI_KEY = "k-env"
        self.assertEqual(
            main._configured_rapidapi_keys(),
            ["k-one", "k-two", "k-env", "k-looter"],
        )
        main.RAPIDAPI_KEY = "k-module"
        self.assertEqual(
            main._configured_rapidapi_keys(),
            ["k-one", "k-two", "k-env", "k-looter", "k-module"],
        )

    def test_nothing_configured_is_the_rapidapi_config_error(self):
        with patch.object(main.http_client, "get", side_effect=AssertionError("called")):
            response = self.client.get("/ai-magic/insta-audit-auto/bluebottle")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "RAPIDAPI_KEY is not configured")
        self.assertEqual(ai_gateway.usage_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
