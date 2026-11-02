# -*- coding: utf-8 -*-
"""V95: passenger direct-driver selection and actuality marker."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("function %s not found" % name)


class DirectDriverSelectionV95(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.keyboards = read("bot/keyboards.py")
        cls.orders = read("bot/order_service.py")
        cls.queue = read("bot/queue_service.py")
        cls.models = read("common/models.py")

    def test_01_entry_menu_is_separate_from_normal_order(self):
        menu = function_source(self.keyboards, "free_drivers_entry_keyboard")
        self.assertIn("Вернуться в главное меню", menu)
        self.assertNotIn("driver_select_list", menu)
        passenger_menu = function_source(self.keyboards, "passenger_menu")
        self.assertIn('"cmd": "new_order"', passenger_menu)
        self.assertIn('"cmd": "drivers"', passenger_menu)
        self.assertIn('"cmd": "driver_select_list"', passenger_menu)
        self.assertIn("👥 Водители", passenger_menu)
        self.assertIn("👤 Выбор водителя", passenger_menu)
        self.assertLess(
            passenger_menu.index('"cmd": "drivers"'),
            passenger_menu.index('"cmd": "driver_select_list"'),
        )

    def test_02_driver_choice_shows_only_name_and_rating(self):
        choices = function_source(self.keyboards, "free_driver_choices_keyboard")
        self.assertIn("driver.id", choices)
        self.assertIn("inline=True", choices)
        self.assertNotIn("car_model", choices)
        self.assertNotIn("car_color", choices)
        self.assertNotIn("car_number", choices)
        listing = function_source(self.handlers, "show_direct_driver_choices")
        self.assertIn("driver.full_name", listing)
        self.assertNotIn("format_rating(driver)", listing)
        self.assertNotIn("driver.car_full", listing)

    def test_03_selection_is_exclusive_for_90_seconds(self):
        select = function_source(self.handlers, "select_direct_driver")
        self.assertIn('entry.status = "offered"', select)
        self.assertIn("DIRECT_DRIVER_TEXT_TIMEOUT", select)
        self.assertIn('"direct_selection"', select)
        self.assertIn("90 секунд", select)
        self.assertIn("class DriverSelection", self.models)
        self.assertIn("unique=True", self.models)

    def test_04_preorder_hold_survives_queue_repairs(self):
        repair = function_source(self.queue, "_repair_visibly_free_drivers")
        self.assertIn("DriverSelection.driver_id", repair)
        statuses = function_source(self.queue, "actual_driver_statuses")
        self.assertIn('result[driver.id] = "considering"', statuses)

    def test_05_direct_request_never_falls_into_fifo(self):
        direct_offer = function_source(self.orders, "offer_to_selected_driver")
        self.assertIn("requested_driver_id", direct_offer)
        self.assertNotIn("next_waiting_driver", direct_offer)
        decline = function_source(self.handlers, "driver_decline")
        direct_branch = decline.split("if order.requested_driver_id:", 1)[1].split("# Explicit handoff", 1)[0]
        self.assertIn('order.status = "cancelled"', direct_branch)
        self.assertIn("Данный водитель не может взять заказ. Попробуйте выбрать другого водителя.", direct_branch)
        self.assertNotIn("offer_to_next_driver", direct_branch)

    def test_06_driver_departure_cancels_direct_instead_of_requeueing(self):
        offline = function_source(self.handlers, "driver_go_offline")
        away = function_source(self.handlers, "driver_go_away")
        helper = function_source(self.handlers, "_cancel_direct_offer_for_unavailable_driver")
        self.assertIn("pending.requested_driver_id", offline)
        self.assertIn("pending.requested_driver_id", away)
        self.assertIn('order.status = "cancelled"', helper)
        self.assertIn("Она не передавалась другим водителям", helper)

    def test_07_normal_orders_keep_existing_fifo_dispatch(self):
        create = function_source(self.handlers, "create_passenger_order")
        self.assertIn("passenger_queue.dispatch_new_order(session, item)", create)
        ordinary = function_source(self.orders, "offer_to_next_driver")
        self.assertIn("queue_service.next_waiting_driver", ordinary)

    def test_08_eta_still_asks_if_passenger_waits(self):
        eta = function_source(self.handlers, "_apply_eta")
        self.assertIn('question = "Ждёте машинку?"', eta)
        self.assertIn("kb.passenger_departure_keyboard(order.id)", eta)
        self.assertNotIn("requested_driver_id", eta)

    def test_09_actuality_confirmation_is_visible_to_driver(self):
        ordinary = function_source(self.orders, "offer_to_next_driver")
        self.assertIn("order.actuality_confirmed", ordinary)
        self.assertIn("Клиент подтвердил актуальность заявки", ordinary)

    def test_10_schema_migration_is_present(self):
        migration = read("migrations/versions/0043_direct_driver_selection.py")
        self.assertIn('down_revision = "0042_route_parallel_offer_state"', migration)
        self.assertIn("driver_selections", migration)
        self.assertIn("requested_driver_id", migration)


if __name__ == "__main__":
    unittest.main()
