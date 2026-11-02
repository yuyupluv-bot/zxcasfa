# -*- coding: utf-8 -*-
"""V121: active bookings can be cancelled safely from the admin panel."""
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


class AdminBookingCancelV121(unittest.TestCase):
    def test_admin_table_has_cancel_action_and_confirmation(self):
        template = read("web/templates/orders.html")
        self.assertIn("url_for('cancel_booking', booking_id=b.id)", template)
        self.assertIn("Отменить бронь", template)
        self.assertIn("data-booking-cancel-form", template)
        self.assertIn("window.confirm", template)

    def test_route_locks_and_delegates_booking(self):
        route = function_source(read("web/app.py"), "cancel_booking")
        self.assertIn(".with_for_update()", route)
        self.assertIn("admin_booking_cancel_service", route)
        self.assertIn("s.commit()", route)
        self.assertIn('"cancel_booking"', route)

    def test_service_closes_all_booking_side_effects(self):
        source = read("bot/admin_booking_cancel_service.py")
        cancel = function_source(source, "cancel_from_admin")
        self.assertIn('booking.status = "canceled"', cancel)
        self.assertIn('booking.canceled_by = "admin"', cancel)
        self.assertIn('ScheduledJob.kind == "booking_chat"', cancel)
        self.assertIn("outbox_service.cancel_or_delete", cancel)
        self.assertIn("cancel_order_from_admin", cancel)
        self.assertIn("booking.driver_id = None", cancel)
        self.assertIn("отменена администратором", cancel)


if __name__ == "__main__":
    unittest.main()
