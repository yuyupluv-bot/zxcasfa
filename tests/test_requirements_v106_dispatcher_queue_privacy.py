# -*- coding: utf-8 -*-
"""V106: ETA card timing and dispatcher-only passenger queue details."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class DispatcherQueuePrivacyV106(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.orders = read("bot/order_service.py")

    def test_accept_notice_does_not_reveal_driver_card(self):
        notice = function_source(self.orders, "notify_driver_found")
        self.assertNotIn("send_tracked_message", notice)
        self.assertNotIn("Водитель взял вашу заявку", notice)
        self.assertIn("cancel_or_delete", notice)

    def test_eta_still_reveals_full_driver_card(self):
        eta = function_source(self.handlers, "_apply_eta")
        self.assertIn("_driver_card(user)", eta)
        self.assertIn("⏰ Будет через", eta)

    def test_dispatcher_details_use_link_and_order_number(self):
        details = function_source(self.handlers, "_dispatcher_passenger_details")
        queue = function_source(self.handlers, "show_queue")
        self.assertIn("_vk_label(passenger)", details)
        self.assertIn("order.number", details)
        self.assertIn("if dispatcher_view else {}", queue)
        self.assertIn("show_passenger_details=dispatcher_view", queue)

    def test_renderer_hides_passenger_from_driver_but_shows_dispatcher(self):
        renderer = function_source(self.handlers, "_render_driver_statuses")
        namespace = {
            "format_rating": lambda _driver: "⭐ 5.0 (10 отзывов)",
            "_display_route_direction": lambda _order: (None, None),
            "_DISPLAY_ORIGIN_LABELS": {"Пашия": "из Пашии"},
            "_DISPLAY_DESTINATION_LABELS": {"Пашия": "до Пашии"},
        }
        exec(
            compile(
                "from __future__ import annotations\n" + renderer,
                "queue_renderer_runtime",
                "exec",
            ),
            namespace,
        )
        driver = SimpleNamespace(
            id=1,
            vk_id=100,
            full_name="Василий Николаевич",
            current_line="Горнозаводск",
        )
        item = {
            "driver": driver,
            "status": "busy",
            "order": object(),
            "position": 1,
            "passenger_label": "[id200|Имя Фамилия]",
            "order_id": 77,
        }
        dispatcher_text = "\n".join(namespace["_render_driver_statuses"](
            [item], numbered=True, show_rating=True, show_passenger_details=True
        ))
        driver_text = "\n".join(namespace["_render_driver_statuses"](
            [item], current_driver_id=1, numbered=True, show_rating=True
        ))
        self.assertIn(
            "Василий Николаевич — на заявке — [id200|Имя Фамилия], "
            "заявка #77",
            dispatcher_text,
        )
        self.assertNotIn("Имя Фамилия", driver_text)
        self.assertNotIn("заявка #77", driver_text)


if __name__ == "__main__":
    unittest.main()
