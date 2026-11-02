# -*- coding: utf-8 -*-
"""V160: file photo uploads, unrestricted positive interval and separated UI."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


def function_source(path: str, name: str) -> str:
    source = read(path)
    node = next(
        item for item in ast.walk(ast.parse(source))
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class RotatingUploadUiV160(unittest.TestCase):
    def test_admin_accepts_verified_image_files(self):
        upload = function_source("web/app.py", "_upload_rotating_photo")
        template = read("web/templates/rotating_posts.html")
        self.assertIn("Image.open", upload)
        self.assertIn("image.verify()", upload)
        self.assertIn('format="JPEG"', upload)
        self.assertIn("upload_message_photo", upload)
        self.assertGreaterEqual(template.count('enctype="multipart/form-data"'), 2)
        self.assertGreaterEqual(template.count('type="file" name="photo"'), 2)
        self.assertIn("image/jpeg,image/png,image/webp", template)

    def test_vk_upload_uses_community_uploader(self):
        upload = function_source("bot/vk_client.py", "upload_message_photo")
        self.assertIn("self.uploader.photo_messages", upload)
        self.assertIn("upload_targets = (None, None, peer_id)", upload)
        self.assertIn("photo_messages(photos=file_path)", upload)
        self.assertIn("access_key", upload)

    def test_interval_accepts_any_positive_minute_value(self):
        app = read("web/app.py")
        service = read("bot/rotating_posts_service.py")
        template = read("web/templates/rotating_posts.html")
        self.assertIn("if interval < 1", app)
        self.assertIn("max(1, get_int", service)
        self.assertIn('type="number" min="1" name="interval"', template)
        self.assertNotIn('max="25" name="interval"', template)

    def test_new_form_and_existing_rotation_are_visually_separate(self):
        template = read("web/templates/rotating_posts.html")
        styles = read("web/static/css/style.css")
        self.assertIn("rotation-create-section", template)
        self.assertIn("Посты в ротации", template)
        self.assertIn("rotation-post-list", template)
        self.assertIn(".rotation-create-section", styles)
        self.assertIn(".rotation-post-card", styles)


if __name__ == "__main__":
    unittest.main()
