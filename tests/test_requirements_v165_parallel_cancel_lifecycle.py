# -*- coding: utf-8 -*-
"""V165: a reserved parallel request cannot replace the current ride."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def function_source(path, name):
    text = source(path)
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(name)


class ParallelCancelLifecycleV165(unittest.TestCase):
    def test_reservation_does_not_set_current_driver_id(self):
        take = function_source("bot/parallel_orders.py", "take")
        self.assertIn("order.parallel_driver_id = driver.id", take)
        self.assertIn("order.driver_id = None", take)
        self.assertLess(
            take.index("order.driver_id = None"),
            take.index('order.status = "parallel_assigned"'),
        )

    def test_promotion_refuses_while_current_ride_is_active(self):
        promote = function_source("bot/parallel_orders.py", "promote_after_current")
        self.assertIn("session.flush()", promote)
        self.assertIn("Blocked premature parallel promotion", promote)
        self.assertIn("if current:", promote)
        self.assertLess(promote.index("if current:"), promote.index('order.status = "assigned"'))

    def test_dispatcher_cancel_restores_current_trip(self):
        cancel = function_source("bot/handlers.py", "disp_cancel_order")
        self.assertIn('previous_status == "parallel_assigned"', cancel)
        self.assertIn("Текущая заявка #{current.number} продолжается", cancel)
        self.assertIn("keyboard=_driver_ride_kb(session, current)", cancel)

    def test_admin_cancel_preserves_busy_driver(self):
        cancel = function_source(
            "bot/admin_order_cancel_service.py", "cancel_from_admin"
        )
        self.assertIn('previous_status == "parallel_assigned"', cancel)
        self.assertIn("reserved_current", cancel)
        self.assertIn("Текущая заявка #{reserved_current.number} продолжается", cancel)

    def test_legacy_reserved_rows_are_repaired_at_startup(self):
        migration = source("common/db_migrate.py")
        self.assertIn(
            "UPDATE orders SET driver_id=NULL WHERE status='parallel_assigned'",
            migration,
        )


if __name__ == "__main__":
    unittest.main()