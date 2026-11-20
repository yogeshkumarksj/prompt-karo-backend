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
    "VIRAL_SONG_ART_ENABLED",
    "VIRAL_SONG_ART_INLINE",
    "VIRAL_SONG_ART_MIN_INTERVAL",
    "VIRAL_SONG_ART_COUNTRY",
    "HOSTINGER_PUBLIC_BASE",
)

ART_100 = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music112/v4/9f/13/ca/cover.jpg/100x100bb.jpg"
)
ART_600 = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music112/v4/9f/13/ca/cover.jpg/600x600bb.jpg"
)
REMIX_ART = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music112/v4/aa/bb/cc/remix.jpg/100x100bb.jpg"
)
PREVIEW = (
    "https://audio-ssl.itunes.apple.com/itunes-assets/AudioPreview211/v4/preview.m4a"
)
TRACK = "https://music.apple.com/us/album/example-song/1635013814?i=1635014240&uo=4"

SONG_KEYS = {
    "rank",
    "title",
    "artist",
    "hashtags",
    "search_text",
    "why_trending",
    "cover_url",
    "preview_url",
    "track_url",
    "reels_count",
    "reels_count_today",
    "reels_count_source",
}


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
        # Lookups stay inside the request and do not sleep. Production leaves
        # both unset: inline is off, and the gap is 3 seconds.
        os.environ["VIRAL_SONG_ART_INLINE"] = "true"
        os.environ["VIRAL_SONG_ART_MIN_INTERVAL"] = "0"
        self.clock = Clock()
        self._now = ai_gateway._now
        ai_gateway._now = self.clock
        ai_gateway.reset_state_for_tests()
        viral_browse.reset_state_for_tests()
        self._delete_gallery()
        self._artwork_on = True
        self._artwork = patch.object(viral_browse, "fetch_song_media", self._empty_media)
        self._artwork.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self._stop_artwork_patch()
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

    async def _empty_media(self, title, artist):
        return viral_browse.empty_song_media()

    def _stop_artwork_patch(self):
        if getattr(self, "_artwork_on", False):
            self._artwork.stop()
            self._artwork_on = False

    def _itunes_payload(self, title, artist, *, match=True):
        if not match:
            return {
                "resultCount": 1,
                "results": [
                    {
                        "kind": "song",
                        "trackName": "Something Else",
                        "artistName": "Someone Else",
                        "artworkUrl100": ART_100,
                        "previewUrl": PREVIEW,
                        "trackViewUrl": TRACK,
                    }
                ],
            }
        return {
            "resultCount": 2,
            "results": [
                {
                    "kind": "song",
                    "trackName": f"{title} (Remix)",
                    "artistName": artist,
                    "artworkUrl100": REMIX_ART,
                    "previewUrl": "https://evil.example/remix.m4a",
                    "trackViewUrl": "https://evil.example/remix",
                },
                {
                    "kind": "song",
                    "trackName": title,
                    "artistName": artist,
                    "artworkUrl100": ART_100,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                },
            ],
        }

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
        self.assertEqual(set(songs[0].keys()), SONG_KEYS)
        self.assertIsNone(songs[0]["cover_url"])
        self.assertIsNone(songs[0]["preview_url"])
        self.assertIsNone(songs[0]["track_url"])
        self.assertIsNone(songs[0]["reels_count"])
        self.assertIsNone(songs[0]["reels_count_today"])
        self.assertIsNone(songs[0]["reels_count_source"])
        self.assertNotIn("cover_source", songs[0])
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
        self.assertEqual(songs[0]["title"], "Shared Anthem")
        self.assertEqual(songs[0]["category"], "hindi-bollywood")
        self.assertEqual(songs[1]["title"], "hindi-bollywood Only")
        self.assertEqual(songs[2]["title"], "punjabi Only")
        self.assertEqual(songs[2]["category"], "punjabi")
        trending_only = next(song for song in songs if song["title"] == "trending Only")
        self.assertGreater(trending_only["rank"], songs[2]["rank"])
        self.assertEqual(shared[0]["category"], "hindi-bollywood")
        self.assertEqual(shared[0]["category_rank"], 1)
        self.assertEqual(shared[0]["rank"], 1)
        self.assertEqual(
            set(shared[0].keys()),
            SONG_KEYS | {"category", "category_rank"},
        )
        self.assertIsNone(shared[0]["cover_url"])
        self.assertIsNone(shared[0]["reels_count"])
        self.assertIsNone(shared[0]["reels_count_source"])
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

    def test_itunes_artwork_matches_the_song_and_upscales_the_cover(self):
        remix_first = self._itunes_payload("Blinding Lights", "The Weeknd")
        media = viral_browse.media_from_itunes_payload(
            "Blinding Lights", "The Weeknd", remix_first
        )
        self.assertEqual(media["cover_url"], ART_600)
        self.assertEqual(media["preview_url"], PREVIEW)
        self.assertEqual(media["track_url"], TRACK)
        self.assertNotIn("remix", media["cover_url"])

        missed = viral_browse.media_from_itunes_payload(
            "Blinding Lights",
            "The Weeknd",
            self._itunes_payload("Blinding Lights", "The Weeknd", match=False),
        )
        self.assertEqual(missed, viral_browse.empty_song_media())

        hostile = {
            "resultCount": 1,
            "results": [
                {
                    "kind": "song",
                    "trackName": "Blinding Lights",
                    "artistName": "The Weeknd",
                    "artworkUrl100": "https://evil.example/100x100bb.jpg",
                    "previewUrl": "http://not-apple.example/preview.m4a",
                    "trackViewUrl": "https://music.apple.com.evil.example/song",
                }
            ],
        }
        cleaned = viral_browse.media_from_itunes_payload(
            "Blinding Lights", "The Weeknd", hostile
        )
        self.assertEqual(cleaned, viral_browse.empty_song_media())

        kesariya = {
            "resultCount": 2,
            "results": [
                {
                    "kind": "song",
                    "trackName": "Kesariya (Lofi Flip)",
                    "artistName": "VIBIE, Arijit Singh & Pritam",
                    "artworkUrl100": REMIX_ART,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                },
                {
                    "kind": "song",
                    "trackName": "Kesariya",
                    "artistName": "Pritam, Arijit Singh & Amitabh Bhattacharya",
                    "artworkUrl100": ART_100,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                },
            ],
        }
        picked = viral_browse.media_from_itunes_payload("Kesariya", "Arijit Singh", kesariya)
        self.assertEqual(picked["cover_url"], ART_600)

        raatan = {
            "resultCount": 2,
            "results": [
                {
                    "kind": "song",
                    "trackName": "Rattan Lamiyan",
                    "artistName": "Kamal Khan",
                    "artworkUrl100": REMIX_ART,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                },
                {
                    "kind": "song",
                    "trackName": 'Raataan Lambiyan (From "Shershaah")',
                    "artistName": "Tanishk Bagchi, Jubin Nautiyal & Asees Kaur",
                    "artworkUrl100": ART_100,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                },
            ],
        }
        matched = viral_browse.media_from_itunes_payload(
            "Raatan Lambiyan", "Jasleen Royal & Jubin Nautiyal", raatan
        )
        self.assertEqual(matched["cover_url"], ART_600)
        self.assertEqual(matched["preview_url"], PREVIEW)
        wrong_artist = {
            "resultCount": 1,
            "results": [
                {
                    "kind": "song",
                    "trackName": "Kya Baat Hai (feat. Pranjal Dahiya)",
                    "artistName": "Bintu Pabra & Manisha Sharma",
                    "artworkUrl100": ART_100,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                }
            ],
        }
        self.assertEqual(
            viral_browse.media_from_itunes_payload("Kya Baat Hai", "Rashmeet Kaur", wrong_artist),
            viral_browse.empty_song_media(),
        )
        senorita = {
            "resultCount": 1,
            "results": [
                {
                    "kind": "song",
                    "trackName": "Señorita",
                    "artistName": "Shawn Mendes & Camila Cabello",
                    "artworkUrl100": ART_100,
                    "previewUrl": PREVIEW,
                    "trackViewUrl": TRACK,
                }
            ],
        }
        self.assertEqual(
            viral_browse.media_from_itunes_payload(
                "Senorita", "Shawn Mendes & Camila Cabello", senorita
            )["cover_url"],
            ART_600,
        )
        self.assertEqual(
            viral_browse._artwork_queries("Raatan Lambiyan", "Jubin Nautiyal")[:2],
            [
                ("Raatan Lambiyan Jubin Nautiyal", "in"),
                ("Raatan Lambiyan", "in"),
            ],
        )

    def test_artwork_rate_gate_spaces_calls(self):
        now = {"t": 0.0}
        sleeps = []

        def monotonic():
            return now["t"]

        async def sleep(delay):
            sleeps.append(delay)
            now["t"] += delay

        gate = viral_browse.IntervalGate(lambda: 3.0, sleep=sleep, monotonic=monotonic)

        async def run():
            self.assertTrue(await gate.wait())
            self.assertTrue(await gate.wait())
            self.assertTrue(await gate.wait())
            gate.stop(10)
            self.assertFalse(await gate.wait())

        asyncio.run(run())
        self.assertEqual(sleeps, [3.0, 3.0])
        os.environ.pop("VIRAL_SONG_ART_MIN_INTERVAL", None)
        os.environ.pop("VIRAL_SONG_ART_INLINE", None)
        self.assertEqual(viral_browse._art_interval(), 3.0)
        self.assertFalse(viral_browse._art_inline())

    def test_cover_is_stored_on_refresh_and_not_looked_up_again_inside_the_window(self):
        self._assert_artwork_follows_window(hours=24, early=6)
        viral_browse.reset_state_for_tests()
        ai_gateway.reset_state_for_tests()
        self.clock.current = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
        self._assert_artwork_follows_window(hours=3, early=2)

    def test_same_song_is_looked_up_once(self):
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append((title, artist))
            return self._itunes_payload(title, artist)

        def builder(prompt):
            return {
                "categories": [
                    {
                        "category_id": category_id,
                        "songs": [
                            {
                                "title": "Shared Anthem",
                                "artist": "Same Artist",
                                "why_trending": "Shows up in every cut.",
                                "hashtags": ["shared"],
                            }
                        ],
                    }
                    for category_id in _category_ids(prompt)
                ]
            }

        self._stop_artwork_patch()
        calls, patcher = self._install(builder)
        with patch.object(viral_browse, "_itunes_search", fake_search), patcher:
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            second = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "punjabi"},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(searches, [("Shared Anthem", "Same Artist")])
        self.assertEqual(first.json()["songs"][0]["cover_url"], ART_600)
        self.assertEqual(first.json()["songs"][0]["preview_url"], PREVIEW)
        self.assertEqual(first.json()["songs"][0]["track_url"], TRACK)
        self.assertEqual(second.json()["songs"][0]["cover_url"], ART_600)
        self.assertEqual(second.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(searches, [("Shared Anthem", "Same Artist")])

    def test_unfinished_cover_lookup_resumes_without_a_model_call(self):
        self._stop_artwork_patch()
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append((title, artist))
            return self._itunes_payload(title, artist)

        # The rest of the batch is already fresh, so this must not call the model.
        # Only Kesariya is still waiting on a cover from an interrupted refresh.
        now = viral_browse._now_ts()
        for category in viral_browse.SONG_CATEGORIES[:6]:
            if category["id"] == "trending":
                items = [
                    {
                        "rank": 1,
                        "title": "Kesariya",
                        "artist": "Arijit Singh",
                        "hashtags": ["reels"],
                        "search_text": "Kesariya Arijit Singh",
                        "why_trending": "Wedding reels still open on this hook.",
                        "cover_url": None,
                        "preview_url": None,
                        "track_url": None,
                        "cover_source": None,
                        "reels_count": 40,
                        "reels_count_today": 3,
                        "reels_count_source": "model",
                    }
                ]
            else:
                items = [
                    {
                        "rank": 1,
                        "title": f"{category['id']} Song",
                        "artist": "Artist",
                        "hashtags": ["reels"],
                        "search_text": f"{category['id']} Song Artist",
                        "why_trending": "Cached.",
                        "cover_url": None,
                        "cover_source": "miss",
                    }
                ]
            viral_browse._cache_put(f"songs:{category['id']}", items, now)
        viral_browse.drop_memory_for_tests()
        with patch.object(viral_browse, "_itunes_search", fake_search):
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            second = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(first.status_code, 200, first.text)
        song = first.json()["songs"][0]
        self.assertEqual(song["cover_url"], ART_600)
        self.assertEqual(song["preview_url"], PREVIEW)
        self.assertEqual(song["track_url"], TRACK)
        self.assertIsNone(song["reels_count"])
        self.assertIsNone(song["reels_count_today"])
        self.assertIsNone(song["reels_count_source"])
        self.assertEqual(searches, [("Kesariya", "Arijit Singh")])
        self.assertEqual(second.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(second.json()["songs"][0]["cover_url"], ART_600)
        self.assertEqual(searches, [("Kesariya", "Arijit Singh")])
        self.assertFalse(any(key.startswith("browse:") for key in ai_gateway.usage_snapshot()))

    def test_previous_itunes_miss_is_retried_once_without_a_model_call(self):
        self._stop_artwork_patch()
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append((title, country))
            if title == "Still Missing":
                return {"resultCount": 0, "results": []}
            return self._itunes_payload(title, artist)

        now = viral_browse._now_ts()
        viral_browse._cache_put(
            "songs:trending",
            [
                {
                    "rank": 1,
                    "title": "Raatan Lambiyan",
                    "artist": "Jubin Nautiyal",
                    "hashtags": ["reels"],
                    "search_text": "Raatan Lambiyan Jubin Nautiyal",
                    "why_trending": "Still here.",
                    "cover_url": None,
                    "cover_source": "none",
                },
                {
                    "rank": 2,
                    "title": "Still Missing",
                    "artist": "Nobody",
                    "hashtags": ["reels"],
                    "search_text": "Still Missing Nobody",
                    "why_trending": "No catalog row.",
                    "cover_url": None,
                    "cover_source": "none",
                },
            ],
            now,
        )
        for category in viral_browse.SONG_CATEGORIES[1:6]:
            viral_browse._cache_put(
                f"songs:{category['id']}",
                [
                    {
                        "rank": 1,
                        "title": f"{category['id']} Song",
                        "artist": "Artist",
                        "hashtags": ["reels"],
                        "search_text": f"{category['id']} Song Artist",
                        "why_trending": "Cached.",
                        "cover_url": ART_600,
                        "preview_url": PREVIEW,
                        "track_url": TRACK,
                        "cover_source": "itunes",
                    }
                ],
                now,
            )
        viral_browse.drop_memory_for_tests()
        with patch.object(viral_browse, "_itunes_search", fake_search):
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            second = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        songs = first.json()["songs"]
        self.assertEqual(songs[0]["cover_url"], ART_600)
        self.assertEqual(songs[0]["preview_url"], PREVIEW)
        self.assertIsNone(songs[1]["cover_url"])
        self.assertIsNone(songs[1]["preview_url"])
        self.assertEqual(second.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(
            [title for title, _country in searches],
            ["Raatan Lambiyan", "Still Missing", "Still Missing", "Still Missing", "Still Missing"],
        )
        self.assertFalse(any(key.startswith("browse:") for key in ai_gateway.usage_snapshot()))
        with patch.object(viral_browse, "_itunes_search", fake_search):
            third = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(len(searches), 5)
        self.assertIsNone(third.json()["songs"][1]["cover_url"])

    def test_old_cache_without_new_fields_still_serves(self):
        self._stop_artwork_patch()
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append(title)
            return self._itunes_payload(title, artist)

        now = viral_browse._now_ts()
        for category in viral_browse.SONG_CATEGORIES[:6]:
            if category["id"] == "trending":
                items = [
                    {
                        "rank": 1,
                        "title": "Old Song",
                        "artist": "Old Artist",
                        "hashtags": ["reels"],
                        "search_text": "Old Song Old Artist",
                        "why_trending": "Still here.",
                        "reels_count": 999,
                        "reels_count_today": 15,
                        "reels_count_source": "guess",
                        "cover_url": "https://evil.example/cover.jpg",
                    }
                ]
            else:
                items = [
                    {
                        "rank": 1,
                        "title": f"{category['id']} Song",
                        "artist": "Artist",
                        "hashtags": ["reels"],
                        "search_text": f"{category['id']} Song Artist",
                        "why_trending": "Cached.",
                    }
                ]
            viral_browse._cache_put(f"songs:{category['id']}", items, now)
        viral_browse.drop_memory_for_tests()
        with patch.object(viral_browse, "_itunes_search", fake_search):
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            again = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        song = response.json()["songs"][0]
        self.assertEqual(song["title"], "Old Song")
        self.assertEqual(song["why_trending"], "Still here.")
        self.assertEqual(set(song.keys()), SONG_KEYS)
        self.assertEqual(song["cover_url"], ART_600)
        self.assertEqual(song["preview_url"], PREVIEW)
        self.assertEqual(song["track_url"], TRACK)
        self.assertIsNone(song["reels_count"])
        self.assertIsNone(song["reels_count_today"])
        self.assertIsNone(song["reels_count_source"])
        self.assertEqual(searches.count("Old Song"), 1)
        self.assertEqual(len(searches), 6)
        self.assertEqual(again.json()["generated_at"], response.json()["generated_at"])
        self.assertEqual(again.json()["songs"][0]["cover_url"], ART_600)
        self.assertFalse(response.json()["stale"])
        self.assertFalse(any(key.startswith("browse:") for key in ai_gateway.usage_snapshot()))

    def test_model_cannot_invent_reel_counts_or_covers(self):
        def builder(prompt):
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
                                "reels_count": 5000,
                                "reels_count_today": 80,
                                "reels_count_source": "model",
                                "cover_url": "https://evil.example/cover.jpg",
                                "preview_url": "https://evil.example/preview.m4a",
                            }
                        ],
                    }
                    for category_id in _category_ids(prompt)
                ]
            }

        calls, patcher = self._install(builder)
        with patcher:
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        song = response.json()["songs"][0]
        self.assertIsNone(song["reels_count"])
        self.assertIsNone(song["reels_count_today"])
        self.assertIsNone(song["reels_count_source"])
        self.assertIsNone(song["cover_url"])
        self.assertIsNone(song["preview_url"])
        self.assertEqual(len(calls), 1)

    def test_itunes_rate_limit_serves_the_list_and_backs_off(self):
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append(title)
            raise viral_browse.ArtworkRateLimit(429)

        self._stop_artwork_patch()
        calls, patcher = self._install(self._simple_songs)
        with patch.object(viral_browse, "_itunes_search", fake_search), patcher:
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            second = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            self.assertEqual(len(searches), 1)
            self.clock.advance(minutes=31)
            third = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["songs"][0]["title"], "trending Song 1")
        self.assertIsNone(first.json()["songs"][0]["cover_url"])
        self.assertIsNone(first.json()["songs"][0]["reels_count"])
        self.assertEqual(second.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(second.json()["songs"][0]["cover_url"], None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(third.json()["generated_at"], first.json()["generated_at"])
        self.assertGreater(len(searches), 1)

    def test_artwork_disabled_skips_the_lookup(self):
        os.environ["VIRAL_SONG_ART_ENABLED"] = "false"
        self._stop_artwork_patch()
        used = []

        async def counting(title, artist):
            used.append(title)
            return viral_browse.empty_song_media()

        calls, patcher = self._install(self._simple_songs)
        with patch.object(viral_browse, "fetch_song_media", counting), patcher:
            response = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "travel"},
            )
            again = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "travel"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["songs"][0]["cover_url"])
        self.assertIsNone(response.json()["songs"][0]["reels_count"])
        self.assertEqual(again.json()["generated_at"], response.json()["generated_at"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(used, [])

    def test_background_lookup_is_not_repeated_on_the_next_request(self):
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake(prompt, schema):
            calls.append(prompt)
            return schema.model_validate(self._simple_songs(prompt)), "groq", "llama-test", 40

        async def slow_media(title, artist):
            slow_media.calls.append((title, artist))
            started.set()
            await release.wait()
            return {
                "cover_url": ART_600,
                "preview_url": PREVIEW,
                "track_url": TRACK,
            }

        slow_media.calls = []

        async def scenario():
            os.environ["VIRAL_SONG_ART_INLINE"] = "false"
            self._stop_artwork_patch()
            with patch.object(ai_gateway, "complete_json", fake), patch.object(
                viral_browse, "fetch_song_media", slow_media
            ):
                first_task = asyncio.create_task(viral_browse.song_top("trending"))
                await asyncio.wait_for(started.wait(), timeout=2)
                first = await asyncio.wait_for(first_task, timeout=2)
                self.assertIsNone(first["songs"][0]["cover_url"])
                self.assertEqual(len(slow_media.calls), 1)
                second = await viral_browse.song_top("sad")
                self.assertIsNone(second["songs"][0]["cover_url"])
                self.assertEqual(len(slow_media.calls), 1)
                self.assertEqual(len(calls), 1)
                release.set()
                await viral_browse.finish_artwork_tasks()
                third = await viral_browse.song_top("trending")
                self.assertEqual(third["songs"][0]["cover_url"], ART_600)
                self.assertEqual(third["songs"][0]["preview_url"], PREVIEW)
                self.assertEqual(third["songs"][0]["track_url"], TRACK)
                self.assertIsNone(third["songs"][0]["reels_count"])
                self.assertEqual(third["generated_at"], first["generated_at"])
                seen = len(slow_media.calls)
                fourth = await viral_browse.song_top("trending")
                self.assertEqual(len(slow_media.calls), seen)
                self.assertEqual(fourth["songs"][0]["cover_url"], ART_600)
                self.assertEqual(len(calls), 1)

        asyncio.run(scenario())

    def _assert_artwork_follows_window(self, hours: int, early: int):
        searches = []

        async def fake_search(title, artist, country=None, term=None):
            searches.append((title, artist))
            return self._itunes_payload(title, artist)

        os.environ["VIRAL_REFRESH_HOURS"] = str(hours)
        os.environ["VIRAL_SONG_ART_INLINE"] = "true"
        os.environ["VIRAL_SONG_ART_MIN_INTERVAL"] = "0"
        self._stop_artwork_patch()
        calls, patcher = self._install(self._simple_songs)
        with patch.object(viral_browse, "_itunes_search", fake_search), patcher:
            first = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            after_first = (len(searches), len(calls))
            self.clock.advance(hours=early)
            middle = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            after_middle = (len(searches), len(calls))
            self.clock.advance(hours=hours - early - 1)
            almost = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            after_almost = (len(searches), len(calls))
            self.clock.advance(hours=1)
            refreshed = self.client.get(
                "/ai-magic/viral-insta-songs/top",
                params={"category": "trending"},
            )
            after_refresh = (len(searches), len(calls))
        self.assertEqual(first.status_code, 200, first.text)
        song = first.json()["songs"][0]
        self.assertEqual(song["cover_url"], ART_600)
        self.assertEqual(song["preview_url"], PREVIEW)
        self.assertEqual(song["track_url"], TRACK)
        self.assertIsNone(song["reels_count"])
        self.assertIsNone(song["reels_count_today"])
        self.assertIsNone(song["reels_count_source"])
        self.assertNotIn("cover_source", song)
        self.assertEqual(after_first, (6, 1))
        self.assertEqual(middle.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(almost.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(middle.json()["songs"][0]["cover_url"], ART_600)
        self.assertEqual(after_middle, (6, 1))
        self.assertEqual(after_almost, (6, 1))
        self.assertNotEqual(refreshed.json()["generated_at"], first.json()["generated_at"])
        self.assertEqual(refreshed.json()["songs"][0]["cover_url"], ART_600)
        self.assertEqual(after_refresh, (12, 2))
        self.assertEqual(first.json()["refresh_hours"], hours)

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
