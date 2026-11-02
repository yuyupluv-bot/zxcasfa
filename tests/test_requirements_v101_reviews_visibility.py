# -*- coding: utf-8 -*-
"""V101: reviews are private to drivers/dispatchers; dispatcher list mirrors queue."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class ReviewsVisibilityV101(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.keyboards = read("bot/keyboards.py")

    def test_01_passenger_lists_hide_reviews_but_driver_card_shows_them(self):
        for name in (
            "show_direct_driver_choices",
            "select_direct_driver",
            "passenger_show_booking",
            "driver_take_booking",
        ):
            self.assertNotIn("format_rating(", function_source(self.handlers, name), name)
        card = function_source(self.handlers, "_driver_card")
        self.assertIn("order_service.driver_card_text(driver)", card)

    def test_02_driver_and_dispatcher_queue_show_reviews(self):
        queue = function_source(self.handlers, "show_queue")
        renderer = function_source(self.handlers, "_render_driver_statuses")
        self.assertIn("show_rating=True", queue)
        self.assertIn("format_rating(driver)", renderer)

    def test_03_dispatcher_free_drivers_is_the_driver_queue(self):
        menu = function_source(self.keyboards, "dispatcher_menu")
        self.assertIn("🚗 Свободные водители", menu)
        self.assertIn('{"cmd": "drivers"}', menu)
        listing = function_source(self.handlers, "show_all_drivers")
        self.assertIn("show_queue(session, user, dispatcher_view=True)", listing)
        router = function_source(self.handlers, "handle_dispatcher")
        self.assertIn('if cmd == "drivers"', router)
        self.assertIn("show_all_drivers(session, user)", router)

    def test_04_driver_own_reviews_remain_available(self):
        reviews = function_source(self.handlers, "show_my_reviews")
        self.assertIn("format_rating(user)", reviews)
        self.assertIn("Ваши отзывы", reviews)

    def test_05_release_uses_v101_image(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()
