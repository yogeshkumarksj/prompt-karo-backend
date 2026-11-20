"""Viral template list, render, and admin upload. No network."""

import io
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/viral-templates-test.db")
os.environ.setdefault("ADMIN_KEY", "test-admin")

from PIL import Image
from fastapi.testclient import TestClient

import main
import viral_templates


def _png(width, height, color):
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ViralTemplateTests(unittest.TestCase):
    def setUp(self):
        main.ADMIN_KEY = "test-admin"
        viral_templates.reset_for_tests()
        self.client = TestClient(main.app)

    def test_list_preview_and_render(self):
        listed = self.client.get("/ai-magic/viral-templates")
        self.assertEqual(listed.status_code, 200)
        templates = listed.json()["templates"]
        self.assertEqual(
            [item["id"] for item in templates],
            ["neon", "bollywood_gold", "minimal_dark", "sunset_gradient"],
        )
        first = templates[0]
        self.assertEqual(first["preview_url"], "/ai-magic/viral-templates/neon/preview.png")
        self.assertEqual((first["width"], first["height"]), (1080, 1920))
        self.assertTrue(first["has_text"])
        self.assertEqual(first["category"], "classic")

        preview = self.client.get(first["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.headers["content-type"].startswith("image/png"))
        self.assertEqual(Image.open(io.BytesIO(preview.content)).size, (1080, 1920))

        rendered = self.client.post(
            "/ai-magic/viral-templates/neon/render",
            data={"text": "Hello"},
            files={"photo": ("me.png", _png(30, 50, (255, 0, 0)), "image/png")},
        )
        self.assertEqual(rendered.status_code, 200)
        image = Image.open(io.BytesIO(rendered.content))
        self.assertEqual(image.size, (1080, 1920))
        self.assertEqual(image.getpixel((540, 590)), (255, 0, 0))
        self.assertNotEqual(image.getpixel((8, 8)), (255, 0, 0))

        story = self.client.get("/ai-magic/story-card/templates")
        self.assertEqual(story.status_code, 200)
        self.assertEqual(len(story.json()["templates"]), 4)

    def test_bad_input_and_rate_limit(self):
        missing = self.client.post("/ai-magic/viral-templates/neon/render", data={"text": "Hi"})
        self.assertEqual(missing.status_code, 400)
        long_text = self.client.post(
            "/ai-magic/viral-templates/neon/render",
            data={"text": "x" * 81},
            files={"photo": ("me.png", _png(8, 8, (1, 2, 3)), "image/png")},
        )
        self.assertEqual(long_text.status_code, 400)

        viral_templates.reset_for_tests()
        viral_templates.RATE_LIMITER.max_calls = 1
        photo = {"photo": ("me.png", _png(8, 8, (9, 9, 9)), "image/png")}
        first = self.client.post("/ai-magic/viral-templates/minimal_dark/render", files=photo)
        second = self.client.post("/ai-magic/viral-templates/minimal_dark/render", files=photo)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_admin_upload_and_delete(self):
        denied = self.client.post("/admin/viral-templates", data={"name": "Nope", "photo_box": "0,0,10,10"})
        self.assertEqual(denied.status_code, 401)
        created = self.client.post(
            "/admin/viral-templates",
            headers={"X-Admin-Key": "test-admin"},
            data={
                "name": "Friday Drop",
                "category": "reels",
                "photo_box": "10,10,40,40",
                "text_box": "10,60,80,30",
                "text_color": "#fc0",
            },
            files={"overlay": ("card.png", _png(120, 160, (20, 20, 20)), "image/png")},
        )
        self.assertEqual(created.status_code, 200)
        template = created.json()["template"]
        self.assertEqual(template["name"], "Friday Drop")
        self.assertEqual(template["width"], 120)
        self.assertTrue(template["has_text"])
        listed = self.client.get("/ai-magic/viral-templates").json()["templates"]
        self.assertEqual(listed[0]["id"], template["id"])

        bad_box = self.client.post(
            "/admin/viral-templates",
            headers={"X-Admin-Key": "test-admin"},
            data={"name": "Bad", "photo_box": "0,0,999,10"},
            files={"overlay": ("card.png", _png(40, 40, (1, 1, 1)), "image/png")},
        )
        self.assertEqual(bad_box.status_code, 400)

        removed = self.client.delete(
            f"/admin/viral-templates/{template['id']}",
            headers={"X-Admin-Key": "test-admin"},
        )
        self.assertEqual(removed.status_code, 200)
        ids = [item["id"] for item in self.client.get("/ai-magic/viral-templates").json()["templates"]]
        self.assertNotIn(template["id"], ids)


if __name__ == "__main__":
    unittest.main()
