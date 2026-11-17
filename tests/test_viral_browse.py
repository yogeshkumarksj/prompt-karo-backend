"""Browse charts for viral songs and AI art. The provider is mocked."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/free-ai-test.db")

import ai_gateway
import models
import viral_browse
from database import SessionLocal
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


ENV_KEYS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "AI_PROVIDER_ORDER",
    "AI_GLOBAL_DAILY_REQUEST_CAP",
    "AI_GLOBAL_MONTHLY_REQUEST_CAP",
    "AI_GLOBAL_DAILY_TOKEN_CAP",
    "AI_GLOBAL_MONTHLY_TOKEN_CAP",
    "AI_FREE_USER_DAILY_LIMIT",
    "AI_BUDGET_ENABLED",
    "AI_BLOCK_PAID_FEATURES",
    "AI_FEATURE_BILLING",
    "VIRAL_REFRESH_HOURS",
    "VIRAL_SONGS_REFRESH_HOURS",
    "VIRAL_ART_REFRESH_HOURS",
    "VIRAL_SONGS_BATCH_SIZE",
    "VIRAL_ART_BATCH_SIZE",
    "VIRAL_BROWSE_DAILY_REQUEST_CAP",
    "VIRAL_BROWSE_DAILY_TOKEN_CAP",
    "VIRAL_BROWSE_RETRY_MINUTES",
    "VIRAL_BROWSE_BACKGROUND",
    "VIRAL_BROWSE_MAX_OUTPUT_TOKENS",
    "HOSTINGER_PUBLIC_BASE",
)


class Clock:
    def __init__(self):
        self.current = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


def _input_payload(prompt: str) -> dict:
    return json.loads(prompt.rsplit("Input:\n", 1)[1])


def _category_ids(prompt: str):
    return [item["id"] for item in _input_payload(prompt)["categories"]]


class ViralBrowseTests(unittest.TestCase):
    def setUp(self):
        self._env = {name: os.environ.get(name) for name in ENV_KEYS}
        for name in ENV_KEYS:
            os.environ.pop(name, None)
        os.environ["GROQ_API_KEY"] = "groq-test-key"
        os.environ["AI_PROVIDER_ORDER"] = "groq"
        self.clock = Clock()
        self._now = ai_gateway._now
        ai_gateway._now = self.clock
        ai_gateway.reset_state_for_tests()
        viral_browse.reset_state_for_tests()
        self._delete_gallery()
        self.client = TestClient(main.app)

    def tearDown(self):
        task = viral_browse._background_task
        viral_browse._background_task = None
        if task is not None and not task.done():
            task.cancel()
        ai_gateway._now = self._now
        self._delete_gallery()
        viral_browse.reset_state_for_tests()
        ai_gateway.reset_state_for_tests()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _delete_gallery(self):
        db = SessionLocal()
        try:
            db.query(models.Prompt).filter(models.Prompt.title.like("vbtest%")).delete(
                synchronize_session=False
            )
            db.commit()
        finally:
            db.close()

    def _install(self, builder):
        calls = []

        async def fake(prompt, schema):
            calls.append(prompt)
            return schema.model_validate(builder(prompt)), "groq", "llama-test", 90

        return calls, patch.object(ai_gateway, "complete_json", fake)

    def test_default_schedule_stays_inside_the_browse_cap(self):
        self.assertEqual(viral_browse.planned_daily_ai_calls(), 16)
        self.assertLessEqual(
            viral_browse.planned_daily_ai_calls(),
            ai_gateway.browse_daily_request_cap(),
        )
        self.assertLess(viral_browse.planned_daily_ai_calls(), 40)
        os.environ["VIRAL_ART_REFRESH_HOURS"] = "12"
        # songs: 2 batches * 4 refreshes, art: 2 batches * 2 refreshes
        self.assertEqual(viral_browse.planned_daily_ai_calls(), 12)

    def test_categories_are_fixed_and_do_not_call_a_provider(self):
        calls, patcher = self._install(lambda _prompt: {"categories": []})
        with patcher:
            songs = self.client.get("/ai-magic/viral-insta-songs/categories")
            art = self.client.get("/ai-magic/viral-ai-art-trends/categories")
        self.assertEqual(songs.status_code, 200, songs.text)
        self.assertEqual(art.status_code, 200, art.text)
        self.assertEqual(calls, [])
        self.assertEqual(
            [item["id"] for item in songs.json()["categories"]],
            [item["id"] for item in viral_browse.SONG_CATEGORIES],
        )
        self.assertEqual(
            [item["label"] for item in art.json()["categories"]],
            [item["label"] for item in viral_browse.ART_CATEGORIES],
        )
        self.assertEqual(songs.json()["all"], {"id": "all", "label": "All"})
        self.assertIn("not a live Instagram scan", songs.json()["disclaimer"])
        self.assertIn("not a live scrape", art.json()["disclaimer"])
        self.assertEqual(songs.json()["billing"], "free")
        self.assertEqual(songs.json()["refresh_hours"], 6)
        self.assertFalse(any(key.startswith("browse:") for key in ai_gateway.usage_snapshot()))

    def test_song_ranks_dedupe_hashtags_and_search_text(self):
        def builder(prompt):
            categories = []
            for category_id in _category_ids(prompt):
                songs = []
                for number in range(1, 23):
                    songs.append(
                        {
                            "title": f"{category_id} Song {number}",
                            "artist": f"Artist {number}",
                            "why_trending": "Used on a lot of reels.",
                            "hashtags": [
                                "#Reels",
                                "reels",
                                "one",
                                "two",
                                "three",
                                "four",
                                "five",
                                "six",
                                "seven",
                            ],
                        }
                    )
                songs.insert(
                    1,
                    {
                        "title": f"  {category_id} song 1  ".upper(),
                        "artist": " artist 1 ",
                        "why_trending": "duplicate",
                        "hashtags": ["extra"],
                    },
                )
                categories.append({"category_id": category_id, "songs": songs})
            return {"categories": categories}

        calls, patcher = self._install(builder)
        with patcher:
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        songs = body["songs"]
        self.assertEqual(len(songs), 20)
        self.assertEqual([song["rank"] for song in songs], list(range(1, 21)))
        self.assertEqual(songs[0]["title"], "trending Song 1")
        self.assertEqual(
            [song["title"] for song in songs].count("trending Song 1")
            + [song["title"] for song in songs].count("TRENDING SONG 1"),
            1,
        )
        self.assertEqual(songs[-1]["title"], "trending Song 20")
        self.assertNotIn("trending Song 21", [song["title"] for song in songs])
        self.assertEqual(songs[0]["search_text"], "trending Song 1 Artist 1")
        self.assertEqual(songs[0]["hashtags"], ["Reels", "one", "two", "three", "four", "five"])
        self.assertLessEqual(len(songs[0]["hashtags"]), 6)
        self.assertEqual(
            set(songs[0].keys()),
            {"rank", "title", "artist", "hashtags", "search_text", "why_trending"},
        )
        self.assertIn("not a live Instagram scan", body["disclaimer"])
        self.assertFalse(body["stale"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(_category_ids(calls[0])), 6)

    def test_all_songs_combines_batches_and_dedupes_across_categories(self):
        def builder(prompt):
            categories = []
            for category_id in _category_ids(prompt):
                categories.append(
                    {
                        "category_id": category_id,
                        "songs": [
                            {
                                "title": "Shared Anthem",
                                "artist": "Same Artist",
                                "why_trending": "Shows up in every cut.",
                                "hashtags": ["shared"],
                            },
                            {
                                "title": f"{category_id} Only",
                                "artist": "Local Artist",
                                "why_trending": "Specific to this lane.",
                                "hashtags": [category_id],
                            },
                        ],
                    }
                )
            return {"categories": categories}

        calls, patcher = self._install(builder)
        with patcher:
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "ALL"},
            )
            again = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "all"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(_category_ids(calls[0])) + len(_category_ids(calls[1])), 12)
        body = response.json()
        songs = body["songs"]
        self.assertEqual(len(songs), 13)
        self.assertEqual([song["rank"] for song in songs], list(range(1, 14)))
        shared = [song for song in songs if song["title"] == "Shared Anthem"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["category"], "trending")
        self.assertEqual(shared[0]["category_rank"], 1)
        self.assertEqual(shared[0]["rank"], 1)
        hindi = next(song for song in songs if song["title"] == "hindi-bollywood Only")
        self.assertEqual(hindi["category"], "hindi-bollywood")
        self.assertEqual(hindi["category_rank"], 2)
        self.assertEqual(body["missing_categories"], [])
        self.assertEqual(body["category"], "all")
        self.assertIn("not a live Instagram scan", body["disclaimer"])
        self.assertEqual(again.json()["generated_at"], body["generated_at"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(ai_gateway.usage_snapshot()["browse:req:day:2026-11-17"], 2)
        self.assertFalse(any(key.startswith("user:") for key in ai_gateway.usage_snapshot()))
        self.assertFalse(any(key.startswith("req:") for key in ai_gateway.usage_snapshot()))

    def test_cache_refreshes_after_six_hours_and_survives_a_restart(self):
        calls, patcher = self._install(self._simple_songs)
        with patcher:
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "sad"},
            )
            self.clock.advance(hours=5, minutes=59)
            second = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "sad"},
            )
            viral_browse.drop_memory_for_tests()
            restarted = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "punjabi"},
            )
            self.clock.advance(minutes=1)
            third = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "sad"},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(second.json()["stale"])
        self.assertEqual(second.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(restarted.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(restarted.json()["songs"][0]["title"], "punjabi Song 1")
        self.assertNotEqual(third.json()["generated_at"], first.json()["generated_at"])
        self.assertFalse(third.json()["stale"])
        # One call for the first batch, none for the restart, one after the window.
        self.assertEqual(len(calls), 2)

    def test_browse_cap_serves_stale_without_calling_the_provider(self):
        calls, patcher = self._install(self._simple_songs)
        with patcher:
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            os.environ["VIRAL_BROWSE_DAILY_REQUEST_CAP"] = "0"
            self.clock.advance(hours=7)
            stale = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(stale.status_code, 200, stale.text)
        body = stale.json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["generated_at"], first.json()["generated_at"])
        self.assertEqual(body["songs"][0]["title"], "trending Song 1")
        self.assertIn("not a live Instagram scan", body["disclaimer"])

    def test_provider_failure_serves_stale_and_does_not_retry_immediately(self):
        state = {"fail": False}
        calls = []

        async def fake(prompt, schema):
            calls.append(prompt)
            if state["fail"]:
                raise HTTPException(status_code=502, detail="provider down")
            return schema.model_validate(self._simple_songs(prompt)), "groq", "llama-test", 90

        with patch.object(ai_gateway, "complete_json", fake):
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            state["fail"] = True
            self.clock.advance(hours=6)
            stale = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            immediate = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            self.assertEqual(len(calls), 2)
            self.clock.advance(minutes=31)
            retried = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(stale.json()["stale"])
        self.assertEqual(stale.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(immediate.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(retried.json()["stale"])
        self.assertEqual(len(calls), 3)

    def test_no_cache_and_no_provider_is_a_friendly_message(self):
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        response = self.client.get(
            "/ai-magic/viral-insta-songs/top",
            params={"category": "travel"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["songs"], [])
        self.assertIn("aren't ready yet", body["message"])
        self.assertIn("not a live Instagram scan", body["disclaimer"])
        self.assertIsNone(body["generated_at"])
        self.assertFalse(any(key.startswith("browse:") for key in ai_gateway.usage_snapshot()))
        self.assertFalse(any(key.startswith("req:") for key in ai_gateway.usage_snapshot()))

    def test_global_off_switch_and_user_pool_are_separate(self):
        calls, patcher = self._install(self._simple_songs)
        with patcher:
            os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "2"
            os.environ["AI_FREE_USER_DAILY_LIMIT"] = "1"
            ai_gateway.begin_generation("viral-insta-songs", "203.0.113.77")
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "fitness-gym"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["songs"])
            with self.assertRaises(HTTPException):
                ai_gateway.begin_generation("viral-insta-songs", "203.0.113.77")
            snap = ai_gateway.usage_snapshot()
            self.assertEqual(snap["req:day:2026-11-17"], 1)
            self.assertEqual(snap["browse:req:day:2026-11-17"], 1)
            user_keys = [key for key in snap if key.startswith("user:")]
            self.assertEqual(len(user_keys), 1)
            self.assertEqual(snap[user_keys[0]], 1)
            # A global cap of 0 is the off switch, including browse.
            os.environ["AI_GLOBAL_DAILY_REQUEST_CAP"] = "0"
            self.clock.advance(hours=6)
            blocked = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "fitness-gym"},
            )
        self.assertEqual(len(calls), 1)
        self.assertTrue(blocked.json()["stale"])
        self.assertEqual(blocked.json()["songs"][0]["title"], response.json()["songs"][0]["title"])

    def test_unknown_category_is_rejected_with_the_disclaimer(self):
        missing = self.client.get("/ai-magic/viral-insta-songs/top")
        self.assertEqual(missing.status_code, 422)
        response = self.client.get(
            "/ai-magic/viral-insta-songs/top",
            params={"category": "not-a-category"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("not a live Instagram scan", response.json()["detail"]["disclaimer"])
        art = self.client.get(
            "/ai-magic/viral-ai-art-trends/browse",
            params={"category": "nope"},
        )
        self.assertEqual(art.status_code, 400, art.text)
        self.assertIn("not a live scrape", art.json()["detail"]["disclaimer"])

    def test_art_browse_dedupes_caps_and_combines_without_ranks(self):
        def builder(prompt):
            categories = []
            for category_id in _category_ids(prompt):
                trends = [
                    {
                        "name": "Golden Hour",
                        "why_it_works": "Warm light reads on a phone.",
                        "prompt": "Warm side light, one subject.",
                        "negative_prompt": "blurry, watermark",
                    }
                ]
                for number in range(1, 15):
                    trends.append(
                        {
                            "name": f"{category_id} Style {number}",
                            "why_it_works": "Clear shapes.",
                            "prompt": f"A {category_id} scene number {number}.",
                            "negative_prompt": "blurry",
                        }
                    )
                trends.insert(
                    2,
                    {
                        "name": "  golden   hour ",
                        "why_it_works": "duplicate",
                        "prompt": "duplicate",
                        "negative_prompt": "blurry",
                    },
                )
                categories.append({"category_id": category_id, "trends": trends})
            return {"categories": categories}

        calls, patcher = self._install(builder)
        with patcher:
            one = self.client.get(
                "/ai-magic/viral-ai-art-trends/browse",
                params={"category": "portrait"},
            )
            combined = self.client.get(
                "/ai-magic/viral-ai-art-trends/browse",
                params={"category": "all"},
            )
        self.assertEqual(one.status_code, 200, one.text)
        trends = one.json()["trends"]
        self.assertEqual(len(trends), 12)
        self.assertEqual(trends[0]["name"], "Golden Hour")
        self.assertEqual(
            [trend["name"] for trend in trends].count("Golden Hour"),
            1,
        )
        self.assertEqual(trends[-1]["name"], "portrait Style 11")
        for trend in trends:
            self.assertNotIn("rank", trend)
            self.assertEqual(
                set(trend.keys()),
                {
                    "id",
                    "name",
                    "category",
                    "prompt",
                    "negative_prompt",
                    "why_it_works",
                    "image_url",
                },
            )
            self.assertIsNone(trend["image_url"])
            self.assertTrue(trend["id"].startswith("portrait-"))
        self.assertIn("not a live scrape", one.json()["disclaimer"])
        # portrait's batch was already cached; all refreshes only the other batch.
        self.assertEqual(len(calls), 2)
        names = [trend["name"] for trend in combined.json()["trends"]]
        self.assertEqual(names.count("Golden Hour"), 1)
        self.assertEqual(combined.json()["trends"][0]["category"], "portrait")
        self.assertTrue(all("rank" not in trend for trend in combined.json()["trends"]))

    def test_art_images_come_from_the_gallery_only(self):
        db = SessionLocal()
        try:
            db.add_all(
                [
                    models.Prompt(
                        title="vbtest Soft window portrait",
                        prompt_text="natural window light portrait",
                        image_url="/uploads/portrait/4.webp",
                        category="Portrait",
                        tags="window,portrait",
                        views=3,
                    ),
                    models.Prompt(
                        title="vbtest Soft window portrait stolen",
                        prompt_text="natural window light portrait",
                        image_url="https://evil.example/pic.webp",
                        category="Portrait",
                        tags="window,portrait,soft",
                        views=999,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        def builder(prompt):
            categories = []
            for category_id in _category_ids(prompt):
                if category_id == "portrait":
                    trends = [
                        {
                            "name": "Soft window portrait",
                            "why_it_works": "Natural light.",
                            "prompt": "Window light portrait.",
                            "negative_prompt": "blurry",
                        },
                        {
                            "name": "Studio portrait",
                            "why_it_works": "Plain backdrop.",
                            "prompt": "Gray studio portrait.",
                            "negative_prompt": "blurry",
                        },
                    ]
                elif category_id == "product":
                    trends = [
                        {
                            "name": "Matte bottle render",
                            "why_it_works": "Clean product shots.",
                            "prompt": "A matte bottle on a table.",
                            "negative_prompt": "text",
                        }
                    ]
                else:
                    trends = [
                        {
                            "name": f"{category_id} glow",
                            "why_it_works": "Looks bright.",
                            "prompt": f"A {category_id} scene.",
                            "negative_prompt": "blurry",
                        }
                    ]
                categories.append({"category_id": category_id, "trends": trends})
            return {"categories": categories}

        calls, patcher = self._install(builder)
        with patcher:
            response = self.client.get(
                "/ai-magic/viral-ai-art-trends/browse",
                params={"category": "all"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 2)
        by_name = {trend["name"]: trend for trend in response.json()["trends"]}
        self.assertEqual(
            by_name["Soft window portrait"]["image_url"],
            "https://antimprompt.com/uploads/portrait/4.webp",
        )
        self.assertIsNone(by_name["Studio portrait"]["image_url"])
        self.assertIsNone(by_name["Matte bottle render"]["image_url"])
        self.assertIn("not a live scrape", response.json()["disclaimer"])

    def test_art_stale_fallback_when_the_browse_cap_is_hit(self):
        def builder(prompt):
            return {
                "categories": [
                    {
                        "category_id": category_id,
                        "trends": [
                            {
                                "name": f"{category_id} glow",
                                "why_it_works": "Looks bright.",
                                "prompt": "One bright scene.",
                                "negative_prompt": "blurry",
                            }
                        ],
                    }
                    for category_id in _category_ids(prompt)
                ]
            }

        calls, patcher = self._install(builder)
        with patcher:
            first = self.client.get(
                "/ai-magic/viral-ai-art-trends/browse",
                params={"category": "anime"},
            )
            os.environ["VIRAL_BROWSE_DAILY_REQUEST_CAP"] = "0"
            self.clock.advance(hours=6)
            stale = self.client.get(
                "/ai-magic/viral-ai-art-trends/browse",
                params={"category": "anime"},
            )
        self.assertEqual(len(calls), 1)
        self.assertTrue(stale.json()["stale"])
        self.assertEqual(stale.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(stale.json()["trends"][0]["name"], "anime glow")
        self.assertNotIn("rank", stale.json()["trends"][0])

    def test_concurrent_requests_share_one_generation(self):
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake(prompt, schema):
            calls.append(prompt)
            started.set()
            await release.wait()
            return schema.model_validate(self._simple_songs(prompt)), "groq", "llama-test", 40

        async def scenario():
            with patch.object(ai_gateway, "complete_json", fake):
                first = asyncio.create_task(viral_browse.song_top("trending"))
                await asyncio.wait_for(started.wait(), timeout=2)
                second = asyncio.create_task(viral_browse.song_top("punjabi"))
                for _ in range(20):
                    await asyncio.sleep(0)
                release.set()
                return await asyncio.gather(first, second)

        trending, punjabi = asyncio.run(scenario())
        self.assertEqual(len(calls), 1)
        self.assertEqual(trending["songs"][0]["title"], "trending Song 1")
        self.assertEqual(punjabi["songs"][0]["title"], "punjabi Song 1")
        self.assertEqual(ai_gateway.usage_snapshot()["browse:req:day:2026-11-17"], 1)

    def test_background_refresh_starts_only_when_enabled(self):
        async def idle():
            viral_browse.maybe_start_background_refresh()
            self.assertIsNone(viral_browse._background_task)

        asyncio.run(idle())

        entered = asyncio.Event()

        async def fake_refresh():
            entered.set()
            await asyncio.sleep(3600)

        async def scenario():
            os.environ["VIRAL_BROWSE_BACKGROUND"] = "true"
            with patch.object(viral_browse, "refresh_due", fake_refresh):
                viral_browse.maybe_start_background_refresh()
                await asyncio.wait_for(entered.wait(), timeout=1)
                await viral_browse.stop_background_refresh()
            self.assertTrue(
                viral_browse._background_task is None or viral_browse._background_task.done()
            )

        asyncio.run(scenario())

    def _simple_songs(self, prompt: str) -> dict:
        return {
            "categories": [
                {
                    "category_id": category_id,
                    "songs": [
                        {
                            "title": f"{category_id} Song 1",
                            "artist": "Artist 1",
                            "why_trending": "Fits this lane.",
                            "hashtags": ["reels"],
                        }
                    ],
                }
                for category_id in _category_ids(prompt)
            ]
        }


if __name__ == "__main__":
    unittest.main()
