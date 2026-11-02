import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DriverChatAutoCancelV48Tests(unittest.TestCase):
    def src(self, rel):
        return (ROOT / rel).read_text("utf-8")

    def test_changed_modules_parse(self):
        for rel in (
            "bot/keyboards.py", "bot/handlers.py", "bot/order_service.py",
            "bot/booking_service.py", "common/settings_service.py",
            "common/config.py", "common/db_migrate.py",
            "migrations/versions/0038_driver_chat_45_minutes.py",
        ):
            ast.parse(self.src(rel), filename=rel)

    def test_manual_no_driver_buttons_are_removed(self):
        keyboards = self.src("bot/keyboards.py")
        booking = keyboards.split("def booking_take_keyboard", 1)[1].split("def dispatcher_reply_keyboard", 1)[0]
        chat = keyboards.split("def chat_take_keyboard", 1)[1].split("def payment_method_keyboard", 1)[0]
        self.assertNotIn("booking_no_driver", booking)
        self.assertNotIn("Никто не захотел", booking)
        self.assertNotIn("chat_no_driver", chat)
        self.assertNotIn("Никто не согласился", chat)
        handlers = self.src("bot/handlers.py")
        allowed = handlers.split("allowed_conversation_cmds =", 1)[1].split("if cmd in allowed_conversation_cmds", 1)[0]
        self.assertNotIn("chat_no_driver", allowed)
        self.assertNotIn("booking_no_driver", allowed)

    def test_all_chat_items_use_45_minutes(self):
        orders = self.src("bot/order_service.py")
        self.assertIn("CHAT_UNCLAIMED_TIMEOUT_SECONDS = 45 * 60", orders)
        self.assertEqual(orders.count('"driver_chat", order.id, CHAT_UNCLAIMED_TIMEOUT_SECONDS'), 2)
        bookings = self.src("bot/booking_service.py")
        self.assertIn("CHAT_UNCLAIMED_TIMEOUT_SECONDS = 45 * 60", bookings)
        handlers = self.src("bot/handlers.py")
        self.assertIn("booking_service.CHAT_UNCLAIMED_TIMEOUT_SECONDS", handlers)

    def test_timeouts_cancel_and_remove_chat_cards(self):
        orders = self.src("bot/order_service.py")
        timeout = orders.split("def _driver_chat_timeout", 1)[1]
        self.assertIn("delete_chat_order_notice(session, order)", timeout)
        self.assertIn('order.status = "cancelled"', timeout)
        bookings = self.src("bot/booking_service.py")
        timeout = bookings.split("def expire_unclaimed_booking", 1)[1].split("def type_label", 1)[0]
        self.assertIn("outbox_service.cancel_or_delete", timeout)
        self.assertIn("session.delete(booking)", timeout)

if __name__ == "__main__":
    unittest.main()
