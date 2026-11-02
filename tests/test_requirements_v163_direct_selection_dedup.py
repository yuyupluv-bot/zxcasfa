# -*- coding: utf-8 -*-
"""V163: one passenger message cannot create a real offer plus a false failure."""
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


class DirectSelectionDedupV163(unittest.TestCase):
    def test_message_identity_wins_over_unstable_event_id(self):
        event_key = function_source("bot/main.py", "_event_key")
        self.assertLess(
            event_key.index("conversation_message_id"),
            event_key.index('explicit = getattr(event, "event_id"'),
        )
        self.assertIn('return f"message:{peer_id}:{message_id}"', event_key)

    def test_user_state_transitions_are_database_serialized(self):
        handler = function_source("bot/handlers.py", "_handle_message_impl")
        user_lookup = handler.split("# Serialize one user's state transitions", 1)[1]
        self.assertIn(".with_for_update()", user_lookup)

    def test_duplicate_order_creation_is_stopped_before_draft_processing(self):
        create = function_source("bot/handlers.py", "create_passenger_order")
        self.assertLess(
            create.index("existing_order = active_order_for"),
            create.index("data = get_data"),
        )
        self.assertIn("Suppressed duplicate passenger order creation", create)
        self.assertIn("return _menu_lock_notice(session, user)", create)

    def test_direct_offer_failure_logs_exact_reason(self):
        offer = function_source("bot/order_service.py", "offer_to_selected_driver")
        self.assertIn("rejection_reasons", offer)
        self.assertIn("queue_status=", offer)
        self.assertIn("driver_status=", offer)
        self.assertIn("Direct offer rejected", offer)


if __name__ == "__main__":
    unittest.main()