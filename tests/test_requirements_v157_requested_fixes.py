# -*- coding: utf-8 -*-
"""V157: delivery queue, expiring menus and stable active-driver controls."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


def function_source(path: str, name: str) -> str:
    source = read(path)
    node = next(
        item for item in ast.walk(ast.parse(source))
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class RequestedFixesV157(unittest.TestCase):
    def test_cancelled_delivery_returns_driver_to_first_position(self):
        decline = function_source("bot/delivery_service.py", "_decline")
        self.assertIn("queue_service.move_to_front(session, driver)", decline)
        self.assertNotIn("queue_service.return_to_queue(session, driver)", decline)

        admin = function_source(
            "bot/admin_order_cancel_service.py", "cancel_from_admin"
        )
        self.assertIn('order.order_type in ("delivery", "partner_delivery")', admin)
        self.assertIn("queue_service.move_to_front(session, driver)", admin)

        dispatcher = function_source("bot/handlers.py", "disp_cancel_order")
        self.assertIn('order.order_type in ("delivery", "partner_delivery")', dispatcher)
        self.assertIn("queue_service.move_to_front(session, driver)", dispatcher)

    def test_delivery_confirmation_menu_is_tracked_and_closed(self):
        submit = function_source("bot/delivery_service.py", "submit_price")
        response = function_source("bot/delivery_service.py", "passenger_response")
        timeout = function_source("bot/delivery_service.py", "_confirm_timeout")
        decline = function_source("bot/delivery_service.py", "_decline")
        helper = function_source(
            "bot/delivery_service.py", "_finish_confirmation_menu"
        )
        self.assertIn("vk.send_tracked_message", submit)
        self.assertIn("_finish_confirmation_menu(session, order)", response)
        self.assertIn("_decline(session, order, passenger, driver)", timeout)
        self.assertIn("_finish_confirmation_menu(session, order)", decline)
        self.assertIn("finalize_tracked_message", helper)

    def test_selected_driver_inline_menu_is_closed(self):
        show = function_source("bot/handlers.py", "show_direct_driver_choices")
        select = function_source("bot/handlers.py", "select_direct_driver")
        close = function_source(
            "bot/handlers.py", "_close_direct_driver_choice_menu"
        )
        self.assertIn("vk.send_tracked_message", show)
        self.assertIn('"selection_menu_outbox_id"', show)
        self.assertIn("_close_direct_driver_choice_menu(session, user)", select)
        self.assertIn("finalize_tracked_message", close)

    def test_driver_counter_offer_buttons_are_absent(self):
        offer = function_source("bot/keyboards.py", "order_offer_keyboard")
        chat = function_source("bot/keyboards.py", "customer_price_chat_keyboard")
        self.assertNotIn("Предложить", offer)
        self.assertNotIn("propose_price", offer)
        self.assertNotIn("Предложить", chat)
        self.assertNotIn("chat_price_offer", chat)

    def test_locked_active_driver_receives_canonical_menu(self):
        lock = function_source("bot/handlers.py", "_menu_lock_notice")
        contact = function_source("bot/handlers.py", "driver_contact_dispatcher")
        handler = function_source("bot/handlers.py", "handle_driver")
        self.assertIn("_active_driver_order_keyboard(session, active)", lock)
        self.assertIn("keyboard=keyboard", lock)
        self.assertIn("_active_driver_order_keyboard(session, order)", contact)
        self.assertIn("return driver_return_to_active(session, user)", handler)


if __name__ == "__main__":
    unittest.main()