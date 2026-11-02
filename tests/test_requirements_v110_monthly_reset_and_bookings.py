# -*- coding: utf-8 -*-
"""V110: safe archive reset and visible/exported bookings."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel): return (ROOT / rel).read_text(encoding="utf-8")

def function_source(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)

class SafeArchiveResetV110(unittest.TestCase):
    def test_bookings_are_visible_and_exported_separately(self):
        template = read("web/templates/orders.html")
        export = function_source(read("web/excel_export.py"), "build_orders_workbook")
        self.assertIn("Предварительные брони", template)
        self.assertIn("Б-{{ b.public_number or b.id }}", template)
        self.assertIn('create_sheet("Брони")', export)

    def test_one_click_builds_excel_then_deletes_same_closed_rows(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn("build_orders_workbook(archived_orders, archived_bookings)", reset)
        self.assertIn("send_file", reset)
        self.assertIn("Order.status.in_(closed_order_statuses)", reset)
        self.assertIn("Booking.status.in_(closed_booking_statuses)", reset)

    def test_active_queues_and_profiles_are_preserved(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertNotIn("s.query(DriverQueue).delete", reset)
        self.assertNotIn("s.query(PassengerQueue).delete", reset)
        self.assertNotIn("s.query(User).delete", reset)
        self.assertNotIn("s.query(Review).delete", reset)
        self.assertIn("PassengerQueue.order_id.in_(order_ids)", reset)
        self.assertIn("Review.order_id.in_(order_ids)", reset)

    def test_false_call_timers_are_preserved(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn('kind != "fakecall"', reset)

    def test_postgres_counters_restart_for_orders_and_bookings(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn("pg_get_serial_sequence", reset)
        self.assertIn("setval", reset)
        self.assertIn("reset_serial_counter(Order)", reset)
        self.assertIn("reset_serial_counter(Booking)", reset)
        self.assertIn("ALTER SEQUENCE", reset)
        self.assertIn("RESTART WITH 1", reset)
        self.assertIn("func.max(model.id)", reset)

    def test_empty_history_still_rewinds_both_counters(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        empty = reset.split("if not archived_orders and not archived_bookings:", 1)[1].split("# Build the download", 1)[0]
        self.assertIn("reset_serial_counter(Order)", empty)
        self.assertIn("reset_serial_counter(Booking)", empty)
        self.assertIn("s.commit()", empty)
        self.assertIn("следующая запись начнётся с #1", empty)

    def test_archive_download_refreshes_stale_admin_page(self):
        template = read("web/templates/orders.html")
        self.assertIn("await fetch(form.action", template)
        self.assertIn("await response.blob()", template)
        self.assertIn("window.location.href", template)
        self.assertIn("reset_done=${Date.now()}", template)

    def test_no_active_work_uses_locked_absolute_restart(self):
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn("LOCK TABLE orders, bookings IN ACCESS EXCLUSIVE MODE", reset)
        self.assertIn("full_reset = not has_active_orders and not has_active_bookings", reset)
        self.assertIn("s.query(Booking).delete", reset)
        self.assertIn("s.query(Order).delete", reset)
        self.assertIn("ALTER SEQUENCE", reset)
        self.assertIn("RESTART WITH 1", reset)

    def test_confirmation_is_required(self):
        template = read("web/templates/orders.html")
        reset = function_source(read("web/app.py"), "reset_orders")
        self.assertIn("window.confirm", template)
        self.assertIn('request.form.get("confirmation") != "yes"', reset)

if __name__ == "__main__": unittest.main()
