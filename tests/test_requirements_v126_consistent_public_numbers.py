# -*- coding: utf-8 -*-
"""V126: every cancellation and booking surface uses the public number."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


class ConsistentPublicNumbersV126(unittest.TestCase):
    def test_passenger_cancel_uses_public_number(self):
        source = read("bot/handlers.py")
        self.assertIn(
            "_continue_after_passenger_order_closed(session, user, order.id, order.number)",
            source,
        )
        self.assertIn("visible_number = closed_order_number or closed_order_id", source)
        self.assertIn("Ваша заявка #{visible_number} отменена", source)
        self.assertNotIn("Ваша заявка #{closed_order_id} отменена", source)

    def test_booking_cancellations_use_public_number(self):
        handlers = read("bot/handlers.py")
        admin = read("bot/admin_booking_cancel_service.py")
        self.assertNotIn("booking_number = booking.id", handlers)
        self.assertNotIn("booking_id = booking.id", admin)
        self.assertIn("booking_number = booking.number", handlers)
        self.assertIn("booking_number = booking.number", admin)

    def test_button_label_is_public_but_payload_stays_internal(self):
        source = read("bot/keyboards.py")
        self.assertIn('№{display_number or booking_id}', source)
        self.assertIn('{"cmd": "booking_take", "booking_id": booking_id}', source)
        self.assertIn('№{display_number or order_id}', source)
        self.assertIn('{"cmd":"chat_take", "order_id":order_id}', source)
        self.assertIn("booking_take_keyboard(booking_id: int, display_number", source)
        self.assertIn("chat_take_keyboard(order_id: int, display_number", source)


if __name__ == "__main__":
    unittest.main()
