# -*- coding: utf-8 -*-
"""V159: rotating posts use community access and do not replay failed backlog."""
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


class CommunityChatAccessV159(unittest.TestCase):
    def test_rotating_peer_never_uses_chat_user_token(self):
        choose = function_source("bot/vk_client.py", "_message_api_for_peer")
        uses = function_source("bot/vk_client.py", "_uses_chat_user_token")
        owned = function_source("bot/vk_client.py", "_is_community_owned_chat")
        self.assertIn("not self._is_community_owned_chat(peer_id)", choose)
        self.assertIn("not self._is_community_owned_chat(peer_id)", uses)
        self.assertIn("rotating_posts_peer_id", owned)

    def test_access_check_uses_community_api_without_sending(self):
        check = function_source("bot/vk_client.py", "check_community_chat_access")
        self.assertIn("self.api.messages.getConversationsById", check)
        self.assertNotIn("messages.send", check)
        app = read("web/app.py")
        template = read("web/templates/rotating_posts.html")
        self.assertIn('"/rotating-posts/check"', app)
        self.assertIn("Проверить доступ к чату", template)

        publish = function_source(
            "bot/rotating_posts_service.py", "publish_due_once"
        )
        self.assertIn("vk.check_community_chat_access(peer_id)", publish)
        self.assertLess(
            publish.index("vk.check_community_chat_access(peer_id)"),
            publish.index("vk.send_message("),
        )

    def test_stale_failed_rows_are_cancelled_before_outbox_starts(self):
        cleanup = function_source(
            "bot/rotating_posts_service.py", "cancel_stale_backlog"
        )
        main = function_source("bot/main.py", "run")
        self.assertIn('"pending", "failed", "sending"', cleanup)
        self.assertIn('OutboxMessage.status: "cancelled"', cleanup)
        self.assertLess(
            main.index("rotating_posts_service.cancel_stale_backlog()"),
            main.index("outbox_service.start_worker()"),
        )


if __name__ == "__main__":
    unittest.main()