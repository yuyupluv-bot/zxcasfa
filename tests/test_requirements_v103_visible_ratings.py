# -*- coding: utf-8 -*-
"""V103: visible driver reviews and star-marked passenger ratings."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class VisibleRatingsV103(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.orders = read("bot/order_service.py")

    def test_driver_card_places_rating_after_link(self):
        card = function_source(self.handlers, "_driver_card")
        canonical = function_source(self.orders, "driver_card_text")
        self.assertIn("order_service.driver_card_text(driver)", card)
        self.assertIn('f"[id{driver.vk_id}|{name}] {driver_rating_text(driver)}\\n"', canonical)
        self.assertIn('f"Авто: {car}."', canonical)

    def test_departure_question_and_answer_are_compact(self):
        eta = function_source(self.handlers, "_apply_eta")
        answer = function_source(self.handlers, "passenger_departure_response")
        self.assertIn("car_prefix + header + card + eta_line,", eta)
        self.assertIn("question,", eta)
        self.assertIn("keyboard=kb.passenger_ride_keyboard(order.id)", eta)
        self.assertNotIn('eta_line + "\\n\\n" + question', eta)
        self.assertNotIn("Вы ответили: Да, жду.", answer)
        self.assertIn('replacement_keyboard=' + repr('{"buttons":[],"inline":true}'), answer)
        self.assertIn("replace_text=True", answer)

    def test_passenger_rating_has_star_after_average(self):
        rating = function_source(self.orders, "passenger_rating_text")
        self.assertIn('passenger.passenger_rating:.1f} ⭐ (', rating)
        self.assertIn('return "— ⭐ (нет отзывов)"', rating)


if __name__ == "__main__":
    unittest.main()
