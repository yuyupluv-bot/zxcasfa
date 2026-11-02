# -*- coding: utf-8 -*-
"""V115: booking cancellation, direct selection, chat and parallel notices."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class RequestedFixesV115(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.bookings = read("bot/booking_service.py")
        cls.keyboards = read("bot/keyboards.py")
        cls.orders = read("bot/order_service.py")
        cls.parallel = read("bot/parallel_orders.py")
        cls.outbox = read("bot/outbox_service.py")

    def test_01_departed_booking_closes_with_linked_order(self):
        helper = function_source(self.bookings, "cancel_for_order")
        self.assertIn("Booking.order_id == int(order_id)", helper)
        self.assertIn(".with_for_update()", helper)
        self.assertIn('booking.status = "canceled"', helper)
        self.assertIn("booking.driver_id = None", helper)
        passenger = function_source(self.handlers, "passenger_cancel")
        driver = function_source(self.handlers, "driver_cancel_active")
        self.assertIn("booking_service.cancel_for_order", passenger)
        self.assertIn("booking_service.cancel_for_order", driver)
        direct_booking_cancel = function_source(self.handlers, "passenger_cancel_booking")
        self.assertNotIn("session.delete(booking)", direct_booking_cancel.split("if was_en_route", 1)[1].split("if driver:", 1)[0])

    def test_02_driver_choice_shows_current_line(self):
        choices = function_source(self.handlers, "show_direct_driver_choices")
        self.assertIn("driver.current_line", choices)
        self.assertIn("линия: {line_name}", choices)
        # VK caps button labels at 40 characters, so the line is rendered in
        # the message list while the payload/name-only buttons stay unchanged.
        keyboard = function_source(self.keyboards, "free_driver_choices_keyboard")
        self.assertNotIn("current_line", keyboard)

    def test_03_direct_timeout_has_both_passenger_messages(self):
        timeout = function_source(self.orders, "_accept_timeout")
        self.assertIn("queue_service.free_drivers(session)", timeout)
        self.assertIn("К сожалению, водитель не смог взять заявку", timeout)
        self.assertIn("можем предложить вам других водителей", timeout)
        self.assertIn("дождитесь свободных водителей и повторите заявку", timeout)
        self.assertIn("kb.direct_driver_unavailable_keyboard()", timeout)
        self.assertIn("kb.passenger_menu()", timeout)

    def test_04_dislike_goes_to_next_fifo_driver(self):
        decline = function_source(self.handlers, "driver_decline")
        branch = decline.split('if cat == "dislike":', 1)[1].split('label = "доставки"', 1)[0]
        self.assertIn("order_service.add_decline", branch)
        self.assertIn('order.status = "searching"', branch)
        self.assertIn("session.flush()", branch)
        self.assertIn("order_service.offer_to_next_driver(session, order)", branch)

    def test_05_far_and_delivery_go_straight_to_chat(self):
        decline = function_source(self.handlers, "driver_decline")
        branch = decline.split('label = "доставки"', 1)[1]
        self.assertIn("publish_special_decline_to_requests_chat", branch)
        self.assertIn("сразу отправлена в чат водителей", branch)
        self.assertNotIn("offer_to_next_driver(session, order)", branch)

    def test_06_claimed_chat_cards_keep_text_but_lose_buttons(self):
        order_final = function_source(self.orders, "finalize_chat_order_notice")
        booking_final = function_source(self.handlers, "_finalize_booking_chat_notice")
        self.assertIn("Заявку взял водитель:", order_final)
        self.assertIn("Бронь взял водитель:", booking_final)
        finalize = function_source(self.outbox, "finalize_tracked_message")
        self.assertIn('\"buttons\":[],\"inline\":true', finalize)
        self.assertIn('final_text = final_text.rstrip() + "\\n\\n" + text', finalize)

    def test_07_gorno_gets_count_only_notice(self):
        refresh = function_source(self.parallel, "refresh_gornozavodsk_parallel_notices")
        self.assertIn("Есть параллельные заявки:", refresh)
        self.assertIn("_russian_request_word(count)", refresh)
        self.assertIn("route_priority_city(row) is None", refresh)
        self.assertIn("_is_gornozavodsk_trip(current)", refresh)
        self.assertNotIn("route_parallel_offer_keyboard", refresh)
        plural = function_source(self.parallel, "_russian_request_word")
        self.assertIn('return "заявки"', plural)

    def test_08_village_route_offer_is_unchanged(self):
        notify = function_source(self.parallel, "notify_busy_drivers")
        self.assertIn("route_priority_city(order)", notify)
        self.assertIn("_claim_auto_offer_trip", notify)
        self.assertIn("kb.route_parallel_offer_keyboard(order.id)", notify)
        for city in ("Пашия", "Кусья", "Чусовой", "Сараны"):
            self.assertIn(f'"{city}"', self.parallel)


if __name__ == "__main__":
    unittest.main()
