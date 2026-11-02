# -*- coding: utf-8 -*-
"""V125: resettable public numbers are independent of permanent DB ids."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel): return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


class PublicNumbersV125(unittest.TestCase):
    def test_models_have_independent_public_sequences(self):
        models = read("common/models.py")
        self.assertIn('Sequence("order_public_number_seq")', models)
        self.assertIn('Sequence("booking_public_number_seq")', models)
        self.assertGreaterEqual(models.count("def number(self) -> int:"), 2)

    def test_migration_creates_and_backfills_public_numbers(self):
        migration = read("migrations/versions/0047_public_numbers.py")
        self.assertIn('revision = "0047_public_numbers"', migration)
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS order_public_number_seq", migration)
        self.assertIn("CREATE SEQUENCE IF NOT EXISTS booking_public_number_seq", migration)
        self.assertIn("UPDATE orders SET public_number=id", migration)
        self.assertIn("UPDATE bookings SET public_number=id", migration)

    def test_reset_restarts_visible_sequences(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn('"order_public_number_seq" if model is Order', reset)
        self.assertIn('"booking_public_number_seq"', reset)
        self.assertIn("ALTER SEQUENCE {public_sequence} RESTART WITH 1", reset)

    def test_admin_and_excel_show_public_numbers(self):
        template = read("web/templates/orders.html")
        export = read("web/excel_export.py")
        self.assertIn("o.public_number or o.id", template)
        self.assertIn("b.public_number or b.id", template)
        self.assertIn("order.number", export)
        self.assertIn("booking.number", export)

    def test_bot_messages_use_public_number_property(self):
        handlers = read("bot/handlers.py")
        orders = read("bot/order_service.py")
        bookings = read("bot/booking_service.py")
        self.assertIn("#{order.number}", handlers)
        self.assertIn("#{order.number}", orders)
        self.assertIn("#{booking.number}", bookings)


if __name__ == "__main__": unittest.main()
