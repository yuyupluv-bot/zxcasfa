"""V131: deterministic queue positions after cancellation and success."""
from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


def function_source(path: str, name: str) -> str:
    source = read(path)
    node = next(
        item for item in ast.parse(source).body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class QueueAfterOrderV131(unittest.TestCase):
    def test_passenger_cancel_moves_active_driver_to_front(self) -> None:
        body = function_source("bot/handlers.py", "passenger_cancel")
        self.assertIn("queue_service.move_to_front(session, driver)", body)

    def test_driver_customer_cancel_moves_to_front(self) -> None:
        body = function_source("bot/handlers.py", "driver_cancel_active")
        branch = body.split('if reason == "customer_cancel":', 1)[1]
        self.assertIn("queue_service.move_to_front(session, user)", branch)

    def test_driver_no_show_moves_to_front(self) -> None:
        body = function_source("bot/handlers.py", "driver_cancel_active")
        branch = body.split('if reason == "no_show":', 1)[1].split('elif reason == "car":', 1)[0]
        self.assertIn("queue_service.move_to_front(session, user)", branch)

    def test_successful_ride_moves_to_tail(self) -> None:
        body = function_source("bot/handlers.py", "driver_complete_ride")
        self.assertIn("queue_service.move_to_tail_after_success(session, user)", body)

    def test_successful_delivery_moves_to_tail(self) -> None:
        body = function_source("bot/handlers.py", "driver_complete_delivery")
        self.assertIn("queue_service.move_to_tail_after_success(session, user)", body)

    def test_parallel_order_is_promoted_instead(self) -> None:
        for name in ("driver_complete_ride", "driver_complete_delivery"):
            body = function_source("bot/handlers.py", name)
            self.assertIn("if not has_parallel:", body)
            self.assertIn("parallel_orders.promote_after_current(session, user)", body)

    def test_tail_helper_is_atomic_and_sets_waiting(self) -> None:
        body = function_source("bot/queue_service.py", "move_to_tail_after_success")
        self.assertIn("with_for_update()", body)
        self.assertIn('entry.status = "waiting"', body)
        self.assertIn("_move_to_tail(session, entry)", body)
        self.assertIn('driver.driver_status = "online"', body)


if __name__ == "__main__":
    unittest.main()
