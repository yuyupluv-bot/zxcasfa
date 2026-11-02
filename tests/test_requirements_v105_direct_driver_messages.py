# -*- coding: utf-8 -*-
"""V105: deterministic direct-driver messages and visible driver rating."""
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


class DirectDriverMessagesV105(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.orders = read("bot/order_service.py")

    def test_selected_driver_sees_passenger_name_while_request_is_typed(self):
        select = function_source(self.handlers, "select_direct_driver")
        self.assertIn('passenger_name = user.full_name or ("id" + str(user.vk_id))', select)
        self.assertIn(
            'f"Вас выбрал пассажир {passenger_name}, заполняет заявку, ожидайте."',
            select,
        )

    def test_direct_request_creation_has_fixed_confirmation(self):
        create = function_source(self.handlers, "create_passenger_order")
        self.assertIn("if selected_driver:", create)
        self.assertIn(
            'f"✅ Заявка #{order.number} создана и отправлена водителю."',
            create,
        )

    def test_accept_notice_contains_only_exact_waiting_text(self):
        notice = function_source(self.orders, "notify_driver_found")
        self.assertNotIn("send_tracked_message", notice)
        self.assertNotIn("Водитель взял вашу заявку", notice)
        self.assertIn("cancel_or_delete", notice)

    def test_driver_card_has_rating_review_count_star_and_car(self):
        rating = function_source(self.orders, "driver_rating_text")
        card = function_source(self.orders, "driver_card_text")
        self.assertIn("driver.rating_count", rating)
        self.assertIn("driver.rating:.1f", rating)
        self.assertIn("⭐", rating)
        self.assertIn("_passenger_review_word(count)", rating)
        self.assertIn("driver_rating_text(driver)", card)
        self.assertIn("driver.car_model", card)
        self.assertIn("driver.car_color", card)
        self.assertIn("driver.car_number", card)

    def test_eta_uses_same_canonical_driver_card(self):
        card = function_source(self.handlers, "_driver_card")
        eta = function_source(self.handlers, "_apply_eta")
        self.assertIn("order_service.driver_card_text(driver)", card)
        self.assertIn("_driver_card(user)", eta)

    def test_runtime_driver_card_exact_output(self):
        code = "from __future__ import annotations\n" + "\n\n".join((
            function_source(self.orders, "_passenger_review_word"),
            function_source(self.orders, "driver_rating_text"),
            function_source(self.orders, "driver_card_text"),
        ))
        namespace = {}
        exec(compile(code, "driver_card_runtime", "exec"), namespace)
        driver = SimpleNamespace(
            vk_id=11243432,
            full_name="Марат Аскаров",
            rating=5.0,
            rating_count=67,
            car_model="Kia Sportage",
            car_color="Белый",
            car_number="444",
        )
        self.assertEqual(
            namespace["driver_card_text"](driver),
            "[id11243432|Марат Аскаров] 5.0 ⭐ (67 отзывов)\n"
            "Авто: Kia Sportage, Белый, 444.",
        )


if __name__ == "__main__":
    unittest.main()
