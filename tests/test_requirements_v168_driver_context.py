# -*- coding: utf-8 -*-
"""V168: requester context, canonical queue and ratings in driver lists."""
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


class DriverContextV168(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")

    def test_old_order_actuality_names_passenger_and_repeats_order(self):
        function = function_source(self.handlers, "passenger_chat_order_actual")
        self.assertIn("Пассажир: {_vk_label(user)} подтвердил: заявка актуальна.", function)
        self.assertIn("Текст заявки: {order_service.order_text(order)}", function)
        self.assertIn("Нажмите «Выезжаю».", function)

    def test_accepted_order_identifies_passenger_or_dispatcher(self):
        helper = function_source(self.handlers, "_order_requester_line")
        self.assertIn("🎧 Заявка от диспетчера", helper)
        self.assertIn("Пассажир: {_vk_label(passenger)}", helper)
        for name in ("driver_take_pending", "driver_accept", "driver_take_from_chat"):
            self.assertIn("_order_requester_line(session, order)", function_source(self.handlers, name))

    def test_who_on_line_reuses_canonical_queue(self):
        function = function_source(self.handlers, "show_who_on_line")
        self.assertIn("show_queue(session, user)", function)

    def test_passenger_driver_list_shows_ratings_and_review_counts(self):
        renderer = function_source(self.handlers, "_render_driver_statuses")
        rating = function_source(read("bot/order_service.py"), "driver_rating_text")
        self.assertIn("show_rating: bool = True", renderer)
        self.assertIn("format_rating(driver)", renderer)
        self.assertIn("driver.rating_count", rating)
        self.assertIn("_passenger_review_word(count)", rating)

    def test_release_image_is_v168(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()