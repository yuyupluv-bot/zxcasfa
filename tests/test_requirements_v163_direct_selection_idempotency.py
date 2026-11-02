# -*- coding: utf-8 -*-
"""V163: repeated VK events cannot invalidate a successful direct order."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")


def function_source(name):
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"function {name} not found")


class DirectSelectionIdempotencyV163(unittest.TestCase):
    def test_existing_direct_order_is_reported_as_success(self):
        helper = function_source("_notice_existing_direct_order")
        self.assertIn("active_order_for(session, user)", helper)
        self.assertIn("order.requested_driver_id", helper)
        self.assertIn("уже создана и отправлена выбранному водителю", helper)
        self.assertIn("kb.passenger_waiting_keyboard(order.id)", helper)

    def test_duplicate_creation_is_stopped_before_new_order(self):
        create = function_source("create_passenger_order")
        guard = create.index("existing_order = active_order_for(session, user)")
        creation = create.index("order = Order(")
        self.assertLess(guard, creation)
        self.assertIn("_notice_existing_direct_order(session, user, requested_driver_id)", create)

    def test_selection_is_rechecked_after_lock_wait(self):
        create = function_source("create_passenger_order")
        invalid = create.index("if not valid_selection:")
        recheck = create.index(
            "_notice_existing_direct_order(session, user, requested_driver_id)",
            invalid,
        )
        unavailable = create.index('"Выбранный водитель уже недоступен. Выберите другого."', invalid)
        self.assertLess(recheck, unavailable)

    def test_repeated_route_text_uses_same_guard(self):
        submit = function_source("direct_driver_submit_text")
        self.assertIn("_notice_existing_direct_order(session, user, requested_driver_id)", submit)


if __name__ == "__main__":
    unittest.main()