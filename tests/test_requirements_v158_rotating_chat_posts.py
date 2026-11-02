# -*- coding: utf-8 -*-
"""V158: persistent fixed-interval round-robin posts to a dedicated VK chat."""
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


class RotatingChatPostsV158(unittest.TestCase):
    def test_schema_and_linear_migration_exist(self):
        model = read("common/models.py")
        migration = read("migrations/versions/0050_rotating_chat_posts.py")
        guard = read("common/db_migrate.py")
        self.assertIn("class RotatingChatPost", model)
        self.assertIn('down_revision = "0049_driver_device_secret"', migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS rotating_chat_posts", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS rotating_chat_posts", guard)

    def test_worker_uses_persistent_round_robin_state(self):
        publish = function_source(
            "bot/rotating_posts_service.py", "publish_due_once"
        )
        self.assertIn("rotating_posts_last_post_id", publish)
        self.assertIn("rotating_posts_last_sent_at", publish)
        self.assertIn("(index + 1) % len(posts)", publish)
        self.assertIn("RotatingChatPost.position.asc()", publish)
        self.assertIn("attachment=post.attachments or None", publish)

    def test_interval_is_fixed_and_positive(self):
        publish = function_source(
            "bot/rotating_posts_service.py", "publish_due_once"
        )
        settings = function_source("web/app.py", "rotating_posts_settings")
        self.assertIn("max(1", publish)
        self.assertNotIn("min(25", publish)
        self.assertIn("interval < 1", settings)

    def test_worker_is_started_and_multi_instance_safe(self):
        main = read("bot/main.py")
        service = read("bot/rotating_posts_service.py")
        self.assertIn("rotating_posts_service.start_worker()", main)
        self.assertIn("pg_try_advisory_xact_lock", service)

    def test_admin_crud_and_media_validation_exist(self):
        app = read("web/app.py")
        template = read("web/templates/rotating_posts.html")
        base = read("web/templates/base.html")
        for route in (
            '"/rotating-posts"',
            '"/rotating-posts/settings"',
            '"/rotating-posts/create"',
            '"/rotating-posts/<int:post_id>/update"',
            '"/rotating-posts/<int:post_id>/delete"',
        ):
            self.assertIn(route, app)
        self.assertIn("(?:photo|video)", app)
        self.assertIn("Посты в чат", base)
        self.assertIn('name="attachments"', template)
        self.assertIn('name="interval"', template)
        self.assertIn('name="peer_id"', template)


if __name__ == "__main__":
    unittest.main()