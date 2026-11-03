"""Fallback behavior for the Instagram Gemini audit. No network calls."""

import asyncio
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/gemini-fallback-test.db")

import main
from fastapi import HTTPException
from google.genai.errors import ClientError, ServerError


SAMPLE_AUDIT = {
    "overall_score": 70,
    "profile": {"score": 70, "reason": "Bio is present."},
    "content": {"score": 60, "reason": "Few recent posts."},
    "engagement": {"score": 50, "reason": "Low likes."},
    "consistency": {"score": 40, "reason": "Irregular posting."},
    "growth_potential": {"score": 65, "reason": "Niche is clear."},
    "strengths": ["clear bio"],
    "weaknesses": ["few posts"],
    "recommendations": ["post on a schedule"],
    "content_strategy": ["short reels"],
    "hashtag_strategy": ["niche tags"],
    "audience_gender": {"available": False, "source": "not_available"},
    "summary": "Solid start.",
}

AUDIT_INPUT = (
    {"username": "tester", "followers": 10},
    [],
    {"average_likes": 0},
    {"available": False},
)


class FakeResponse:
    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text


class ScriptedModels:
    def __init__(self, script, calls, configs):
        self._script = list(script)
        self.calls = calls
        self.configs = configs

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        self.configs.append(config)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ScriptedClient:
    def __init__(self, script, calls, configs):
        self.models = ScriptedModels(script, calls, configs)

    def __call__(self, api_key=None):
        return self


def api_error(cls, code, status, message):
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


class GeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("GEMINI_MODELS")
        os.environ.pop("GEMINI_MODELS", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("GEMINI_MODELS", None)
        else:
            os.environ["GEMINI_MODELS"] = self._env

    def test_default_model_list_matches_documented_flash_models(self):
        self.assertEqual(
            main.gemini_model_chain(),
            ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )

    def test_env_override_strips_and_dedupes(self):
        os.environ["GEMINI_MODELS"] = " gemini-3.5-flash-lite, gemini-3.8-flash, gemini-3.5-flash-lite "
        self.assertEqual(
            main.gemini_model_chain(),
            ["gemini-3.5-flash-lite", "gemini-3.8-flash"],
        )

    def test_blank_env_uses_defaults(self):
        os.environ["GEMINI_MODELS"] = " , "
        self.assertEqual(
            main.gemini_model_chain(),
            ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )

    def test_transient_and_fatal_classification(self):
        transient = [
            api_error(ServerError, 503, "UNAVAILABLE", "This model is currently experiencing high demand"),
            api_error(ClientError, 429, "RESOURCE_EXHAUSTED", "quota"),
            api_error(ServerError, 500, "INTERNAL", "internal"),
            api_error(ServerError, 504, "DEADLINE_EXCEEDED", "deadline exceeded"),
            TimeoutError("The read operation timed out"),
            Exception("503 UNAVAILABLE: This model is currently experiencing high demand"),
            api_error(ClientError, 404, "NOT_FOUND", "models/gemini-3.6-flash is not found"),
        ]
        for exc in transient:
            self.assertTrue(main.is_transient_gemini_error(exc), repr(exc))

        fatal = [
            api_error(ClientError, 401, "UNAUTHENTICATED", "API key not valid"),
            api_error(ClientError, 403, "PERMISSION_DENIED", "Your API key was reported as leaked"),
            api_error(ClientError, 400, "INVALID_ARGUMENT", "bad request"),
            Exception("not a model problem"),
        ]
        for exc in fatal:
            self.assertFalse(main.is_transient_gemini_error(exc), repr(exc))

    def _run(self, script, models=None):
        calls = []
        configs = []
        client = ScriptedClient(script, calls, configs)
        if models is not None:
            os.environ["GEMINI_MODELS"] = models
        with patch.object(main.genai, "Client", client), patch.object(main.time, "sleep") as sleep:
            try:
                result = main.generate_ai_audit(*AUDIT_INPUT)
            except HTTPException as exc:
                return exc, calls, sleep
        return result, calls, sleep

    def test_primary_success_keeps_prompt_and_schema(self):
        report = main.AuditReport.model_validate(SAMPLE_AUDIT)
        result, calls, sleep = self._run([FakeResponse(parsed=report)])
        self.assertIs(result[0], report)
        self.assertEqual(result[1], "gemini-3.6-flash")
        self.assertEqual([call["model"] for call in calls], ["gemini-3.6-flash"])
        sleep.assert_not_called()
        contents = calls[0]["contents"]
        self.assertIn("You are an expert Instagram growth and content strategist.", contents)
        self.assertIn("Never infer gender from names, usernames, profile photos, captions, or any other proxy.", contents)
        self.assertEqual(calls[0]["config"]["response_mime_type"], "application/json")
        self.assertIs(calls[0]["config"]["response_schema"], main.AuditReport)

    def test_retries_overloaded_primary_then_uses_it(self):
        report = main.AuditReport.model_validate(SAMPLE_AUDIT)
        overload = api_error(
            ServerError,
            503,
            "UNAVAILABLE",
            "This model is currently experiencing high demand",
        )
        result, calls, sleep = self._run([overload, FakeResponse(parsed=report)])
        self.assertEqual(result[1], "gemini-3.6-flash")
        self.assertEqual([call["model"] for call in calls], ["gemini-3.6-flash", "gemini-3.6-flash"])
        sleep.assert_called_once_with(main.GEMINI_SAME_MODEL_RETRY_SECONDS)

    def test_falls_back_after_primary_stays_unavailable(self):
        report = main.AuditReport.model_validate(SAMPLE_AUDIT)
        overload = api_error(ServerError, 503, "UNAVAILABLE", "high demand")
        text = main.AuditReport.model_validate(SAMPLE_AUDIT).model_dump_json()
        result, calls, sleep = self._run(
            [overload, overload, FakeResponse(parsed=None, text=text)],
            models="gemini-3.6-flash,gemini-3.8-flash",
        )
        self.assertEqual(result[1], "gemini-3.8-flash")
        self.assertEqual(result[0].overall_score, 70)
        self.assertEqual(
            [call["model"] for call in calls],
            ["gemini-3.6-flash", "gemini-3.6-flash", "gemini-3.8-flash"],
        )
        sleep.assert_called_once_with(main.GEMINI_SAME_MODEL_RETRY_SECONDS)

    def test_missing_model_skips_retry_and_uses_next(self):
        report = main.AuditReport.model_validate(SAMPLE_AUDIT)
        missing = api_error(ClientError, 404, "NOT_FOUND", "model not found")
        result, calls, sleep = self._run(
            [missing, FakeResponse(parsed=report)],
            models="gemini-3.6-flash,gemini-3.5-flash-lite",
        )
        self.assertEqual(result[1], "gemini-3.5-flash-lite")
        self.assertEqual(
            [call["model"] for call in calls],
            ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
        )
        sleep.assert_not_called()

    def test_invalid_key_fails_fast(self):
        leaked = api_error(
            ClientError,
            403,
            "PERMISSION_DENIED",
            "Your API key was reported as leaked",
        )
        bad_request = api_error(ClientError, 400, "INVALID_ARGUMENT", "bad request")
        for exc, needle in ((leaked, "API key was rejected"), (bad_request, "rejected as invalid")):
            error, calls, sleep = self._run(
                [exc, FakeResponse(parsed=main.AuditReport.model_validate(SAMPLE_AUDIT))],
                models="gemini-3.6-flash,gemini-3.8-flash",
            )
            self.assertIsInstance(error, HTTPException)
            self.assertEqual(error.status_code, 502)
            self.assertIn(needle, error.detail)
            self.assertNotIn("all models were unavailable", error.detail)
            self.assertEqual([call["model"] for call in calls], ["gemini-3.6-flash"])
            sleep.assert_not_called()

    def test_all_models_unavailable_returns_502(self):
        overload = api_error(
            ServerError,
            503,
            "UNAVAILABLE",
            "This model is currently experiencing high demand",
        )
        error, calls, sleep = self._run(
            [overload, overload, overload, overload],
            models="gemini-3.6-flash,gemini-3.8-flash",
        )
        self.assertIsInstance(error, HTTPException)
        self.assertEqual(error.status_code, 502)
        self.assertIn("all models were unavailable", error.detail)
        self.assertIn("gemini-3.6-flash", error.detail)
        self.assertIn("gemini-3.8-flash", error.detail)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleep.call_count, 2)

    def test_error_detail_redacts_api_key(self):
        os.environ["GEMINI_API_KEY"] = "super-secret-key-value"
        overload = Exception("503 UNAVAILABLE super-secret-key-value high demand")
        error, _, _ = self._run(
            [overload, overload],
            models="gemini-3.6-flash",
        )
        self.assertNotIn("super-secret-key-value", error.detail)
        self.assertIn("[redacted]", error.detail)
        os.environ.pop("GEMINI_API_KEY", None)

    def test_run_audit_job_reports_model_without_changing_audit_shape(self):
        profile = {
            "username": "tester",
            "full_name": "Test User",
            "biography": "bio",
            "edge_followed_by": {"count": 12},
            "edge_follow": {"count": 3},
            "edge_owner_to_timeline_media": {"count": 0, "edges": []},
            "is_private": False,
        }
        report = main.AuditReport.model_validate(SAMPLE_AUDIT)

        async def fake_profile(username):
            return profile

        calls = []
        client = ScriptedClient([FakeResponse(parsed=report)], calls, [])
        with patch.object(main, "get_instagram_profile_async", fake_profile), patch.object(
            main.genai, "Client", client
        ), patch.object(main.time, "sleep"):
            result = asyncio.run(main.run_audit_job("tester"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["username"], "tester")
        self.assertEqual(result["ai_audit_model"], "gemini-3.6-flash")
        self.assertEqual(result["ai_audit"]["overall_score"], 70)
        self.assertNotIn("ai_audit_model", result["ai_audit"])
        for key in ("profile", "recent_posts", "engagement", "follower_gender", "ai_audit"):
            self.assertIn(key, result)

    def test_private_account_does_not_call_gemini(self):
        profile = {
            "username": "tester",
            "is_private": True,
            "edge_followed_by": {"count": 1},
            "edge_follow": {"count": 1},
            "edge_owner_to_timeline_media": {"count": 0, "edges": []},
        }

        async def fake_profile(username):
            return profile

        with patch.object(main, "get_instagram_profile_async", fake_profile), patch.object(
            main, "generate_ai_audit", side_effect=AssertionError("gemini should not run")
        ):
            result = asyncio.run(main.run_audit_job("tester"))

        self.assertEqual(result["status"], "private")
        self.assertNotIn("ai_audit", result)
        self.assertNotIn("ai_audit_model", result)


if __name__ == "__main__":
    unittest.main()
