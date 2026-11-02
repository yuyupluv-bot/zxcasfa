# -*- coding: utf-8 -*-
"""V133: passenger can replace the fare card during its ten-minute lifetime."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def function_source(source, name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


class CustomerPriceChangeV133(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.service = read("bot/customer_price_service.py")
        cls.orders = read("bot/order_service.py")
        cls.keyboards = read("bot/keyboards.py")

    def test_created_message_and_two_button_menu(self):
        create = function_source(self.handlers, "create_passenger_order")
        self.assertIn("Ждем когда первый водитель", create)
        self.assertIn("Вы можете изменить цену", create)
        menu = function_source(self.keyboards, "customer_price_waiting_keyboard")
        self.assertIn("Изменить цену", menu)
        self.assertIn("Жду первого водителя", menu)

    def test_passenger_price_edit_is_routed_before_generic_waiting_text(self):
        handle = function_source(self.handlers, "handle_passenger")
        self.assertIn('if cmd == "customer_price_edit":', handle)
        self.assertIn("submit_passenger_price_edit", handle)
        self.assertIn("States.P_CUSTOM_PRICE_EDIT", handle)

    def test_replacement_starts_with_change_and_deletes_old_card(self):
        replace = function_source(self.orders, "replace_customer_price_chat_notice")
        self.assertIn("Клиент изменил цену ({old_price} руб. → {new_price} руб.)", replace)
        self.assertLess(replace.index("Клиент изменил цену"), replace.index("🔔 Заявка"))
        self.assertIn("outbox_service.cancel_or_delete(session, old_outbox_id)", replace)
        self.assertIn("order.chat_notice_outbox_id = new_outbox_id", replace)

    def test_price_change_does_not_restart_expiry(self):
        submit = function_source(self.service, "submit_passenger_price_edit")
        replace = function_source(self.orders, "replace_customer_price_chat_notice")
        self.assertNotIn("timers.schedule", submit)
        self.assertNotIn("timers.schedule", replace)

    def test_customer_price_request_expires_after_ten_minutes(self):
        self.assertIn("CUSTOMER_PRICE_UNCLAIMED_TIMEOUT_SECONDS = 10 * 60", self.orders)
        publish = function_source(self.orders, "publish_direct_to_requests_chat")
        self.assertIn("CUSTOMER_PRICE_UNCLAIMED_TIMEOUT_SECONDS", publish)
        timeout = function_source(self.orders, "_driver_chat_timeout")
        self.assertIn('order.status = "cancelled"', timeout)
        self.assertIn('order.cancelled_by = "system"', timeout)

    def test_price_is_recognized_from_150_without_rejecting_address_numbers(self):
        self.assertIn("MIN_PRICE = 150", self.service)
        self.assertNotIn("def has_low_customer_price", self.service)
        self.assertNotIn("Минимальная цена — 150 рублей.", self.service)
        addresses = function_source(self.handlers, "order_set_addresses")
        self.assertNotIn("customer_price_service.has_low_customer_price(raw)", addresses)
        self.assertNotIn("Минимальная цена — 150 рублей.", addresses)



if __name__ == "__main__":
    unittest.main()
