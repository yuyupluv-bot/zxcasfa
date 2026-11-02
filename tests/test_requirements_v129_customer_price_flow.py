# -*- coding: utf-8 -*-
"""V129: complete driver counter-offer flow in private messages and driver chat."""
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


class CustomerPriceFlowV129(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.service = read("bot/customer_price_service.py")
        cls.keyboards = read("bot/keyboards.py")
        cls.queue = read("bot/queue_service.py")

    def test_private_offer_button_opens_price_prompt(self):
        handle = function_source(self.handlers, "handle_driver")
        self.assertIn('if cmd == "propose_price":', handle)
        self.assertIn("customer_price_service.start_from_private_offer", handle)
        start = function_source(self.service, "start_from_private_offer")
        self.assertIn("Какую цену вы хотите предложить клиенту", start)

    def test_typed_amount_is_processed_before_offer_lock(self):
        handle = function_source(self.handlers, "handle_driver")
        submit = handle.index("customer_price_service.submit(session, user, text)")
        lock = handle.index("if pending_offer and cmd not in _DRIVER_OFFER_ALLOWED_CMDS")
        self.assertLess(submit, lock)

    def test_driver_chat_offer_is_reserved_and_prompts_privately(self):
        reserve = function_source(self.service, "reserve_from_chat")
        self.assertIn("Какую цену вы хотите предложить клиенту", reserve)
        self.assertIn("States.D_CUSTOM_PRICE", reserve)
        self.assertIn("considering_price=True", reserve)

    def test_passenger_receives_requested_copy_and_buttons(self):
        self.assertIn("Водитель согласен вас отвезти за {amount} рублей.", self.service)
        confirm = function_source(self.keyboards, "customer_price_confirm_keyboard")
        self.assertIn("✅ Да, поеду", confirm)
        self.assertIn("❌ Нет, отменяю заявку", confirm)

    def test_passenger_has_exactly_90_seconds(self):
        self.assertIn("COUNTER_OFFER_TIMEOUT_SECONDS = 90", self.service)
        self.assertIn('timers.schedule("customer_price"', self.service)
        self.assertIn('timers.cancel("customer_price"', self.service)

    def test_driver_is_considering_and_cannot_receive_another_order(self):
        self.assertIn("def mark_considering", self.queue)
        self.assertIn('entry.status = "offered"', function_source(self.queue, "mark_considering"))
        self.assertGreaterEqual(self.service.count("queue_service.mark_considering"), 2)
        self.assertNotIn("queue_service.mark_assigned", self.service)

    def test_queue_says_considering_not_on_ride(self):
        render = function_source(self.handlers, "_render_driver_statuses")
        self.assertIn('if status == "considering":', render)
        self.assertIn("рассматривает заявку", render)
        show_queue = function_source(self.handlers, "show_queue")
        self.assertIn('display_status = "considering"', show_queue)
        self.assertIn("Вы сейчас рассматриваете заявку", show_queue)

    def test_yes_leads_to_eta_then_driver_car_notification(self):
        accept = function_source(self.service, "_accept")
        self.assertIn("notify_passenger=False", accept)
        self.assertIn("reserved_price_offer=True", accept)
        driver_accept = function_source(self.handlers, "driver_accept")
        self.assertIn("_show_eta_menu(session, user, order)", driver_accept)
        apply_eta = function_source(self.handlers, "_apply_eta")
        self.assertIn("departure_driver_card_text(assigned_driver)", apply_eta)
        self.assertIn("_driver_card(user)", apply_eta)
        self.assertIn("⏰ Будет через {eta} мин.", apply_eta)

    def test_no_or_timeout_cancels_and_releases_driver(self):
        cancel = function_source(self.service, "_cancel")
        self.assertIn('order.status = "cancelled"', cancel)
        self.assertIn("queue_service.restore_position", cancel)
        timeout = function_source(self.service, "timeout")
        self.assertIn("Клиент не ответил за 90 секунд", timeout)


if __name__ == "__main__":
    unittest.main()
