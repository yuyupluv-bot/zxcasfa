# -*- coding: utf-8 -*-
"""V162: rotating posts keep links clickable without VK preview cards."""
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


class RotatingLinkPreviewV162(unittest.TestCase):
    def test_migration_and_model_persist_flag(self):
        migration = read("migrations/versions/0051_outbox_dont_parse_links.py")
        model = read("common/models.py")
        guard = read("common/db_migrate.py")
        self.assertIn('down_revision = "0050_rotating_chat_posts"', migration)
        self.assertIn("dont_parse_links", migration)
        self.assertIn("dont_parse_links = Column(Boolean", model)
        self.assertIn("ADD COLUMN IF NOT EXISTS dont_parse_links", guard)

    def test_rotating_posts_disable_only_link_preview(self):
        publish = function_source(
            "bot/rotating_posts_service.py",
            "publish_due_once",
        )
        self.assertIn("dont_parse_links=True", publish)

    def test_outbox_delivers_vk_parameter(self):
        send = function_source("bot/vk_client.py", "send_message")
        send_now = function_source("bot/vk_client.py", "_send_now_result")
        delivery = function_source("bot/outbox_service.py", "_deliver_one")
        self.assertIn("dont_parse_links=bool(dont_parse_links)", send)
        self.assertIn('params["dont_parse_links"] = 1', send_now)
        self.assertIn("bool(row.dont_parse_links)", delivery)


if __name__ == "__main__":
    unittest.main()