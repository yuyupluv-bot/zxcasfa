# -*- coding: utf-8 -*-
"""V170: 150-ruble minimum and precise passenger dispatch messages."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


class DispatchMessagesV170(unittest.TestCase):
    def test_price_recognition_range_starts_at_150(self):
        service = read("bot/customer_price_service.py")
        self.assertIn("MIN_PRICE = 150", service)
        self.assertIn("MIN_PRICE <= value <= MAX_PRICE", service)
        self.assertNotIn("MIN_PRICE = 120", service)

    def test_wait_confirmation_is_unambiguous(self):
        function = function_source(read("bot/handlers.py"), "passenger_arrived_reply")
        self.assertIn("Пассажир просит подождать.", function)
        self.assertIn("Водитель уведомлён: вы попросили его подождать.", function)
        self.assertNotIn("Вы передали водителю, что нужно подождать", function)

    def test_new_order_reports_free_driver_handoff(self):
        function = function_source(read("bot/passenger_queue.py"), "dispatch_new_order")
        self.assertIn("⏰ Минутку…", function)

    def test_no_free_driver_reports_parallel_search(self):
        function = function_source(read("bot/order_service.py"), "_handle_no_driver")
        self.assertIn("⏰ Чуть подождать, первый свободный, возьмет вашу заявку…", function)

    def test_single_order_has_no_generic_created_notice(self):
        function = function_source(read("bot/handlers.py"), "create_passenger_order")
        self.assertNotIn("send_bot_message(", function)

    def test_release_image_is_v170(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()