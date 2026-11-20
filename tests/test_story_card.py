"""Story card templates and a cover-free render. No network."""

import io
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/story-card-test.db")

from PIL import Image
from fastapi.testclient import TestClient

import main
import story_card


class StoryCardTests(unittest.TestCase):
    def setUp(self):
        story_card.reset_for_tests()
        self.client = TestClient(main.app)

    def test_templates_and_preview(self):
        response = self.client.get("/ai-magic/story-card/templates")
        self.assertEqual(response.status_code, 200)
        templates = response.json()["templates"]
        self.assertEqual(
            [item["id"] for item in templates],
            ["neon", "bollywood_gold", "minimal_dark", "sunset_gradient"],
        )
        preview_url = templates[0]["preview_url"]
        self.assertEqual(preview_url, "/ai-magic/story-card/templates/neon/preview.png")
        preview = self.client.get(preview_url)
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.headers["content-type"].startswith("image/png"))
        image = Image.open(io.BytesIO(preview.content))
        self.assertLess(image.size[0], 1080)
        self.assertLess(image.size[1], 1920)

    def test_post_without_cover_or_photo_is_story_png(self):
        response = self.client.post(
            "/ai-magic/story-card",
            files={
                "template_id": (None, "neon"),
                "song_title": (None, "Kesariya"),
                "artist": (None, "Arijit Singh"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/png"))
        image = Image.open(io.BytesIO(response.content))
        self.assertEqual(image.size, (1080, 1920))

    def test_bad_input_is_400_and_limit_is_429(self):
        missing = self.client.post(
            "/ai-magic/story-card",
            files={"template_id": (None, "nope"), "song_title": (None, "A"), "artist": (None, "B")},
        )
        self.assertEqual(missing.status_code, 400)
        long_caption = self.client.post(
            "/ai-magic/story-card",
            files={
                "template_id": (None, "minimal_dark"),
                "song_title": (None, "A"),
                "artist": (None, "B"),
                "caption": (None, "x" * 81),
            },
        )
        self.assertEqual(long_caption.status_code, 400)
        bad_host = self.client.post(
            "/ai-magic/story-card",
            files={
                "template_id": (None, "sunset_gradient"),
                "song_title": (None, "A"),
                "artist": (None, "B"),
                "cover_url": (None, "https://example.com/cover.jpg"),
            },
        )
        self.assertEqual(bad_host.status_code, 400)

        story_card.reset_for_tests()
        story_card.RATE_LIMITER.max_calls = 1
        first = self.client.post(
            "/ai-magic/story-card",
            files={
                "template_id": (None, "bollywood_gold"),
                "song_title": (None, "A"),
                "artist": (None, "B"),
            },
        )
        second = self.client.post(
            "/ai-magic/story-card",
            files={
                "template_id": (None, "bollywood_gold"),
                "song_title": (None, "A"),
                "artist": (None, "B"),
            },
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()
