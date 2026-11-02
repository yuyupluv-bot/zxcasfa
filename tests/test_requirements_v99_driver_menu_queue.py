# -*- coding: utf-8 -*-
"""V99: separate passenger buttons and detailed numbered driver queue."""
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
    raise AssertionError(f"function {name} not found")


class PassengerButtonsV99(unittest.TestCase):
    def test_drivers_and_selection_are_separate_neighbour_buttons(self):
        menu = function_source(read("bot/keyboards.py"), "passenger_menu")
        self.assertIn("👥 Водители", menu)
        self.assertIn("👤 Выбор водителя", menu)
        self.assertIn('{"cmd": "drivers"}', menu)
        self.assertIn('{"cmd": "driver_select_list", "page": 1}', menu)
        self.assertLess(menu.index('{"cmd": "drivers"}'), menu.index('{"cmd": "driver_select_list"'))

    def test_old_saved_label_is_migrated(self):
        self.assertIn('"btn_drivers": "👥 Водители"', read("common/settings_service.py"))
        migration = read("common/db_migrate.py")
        self.assertIn("SET value = '👥 Водители'", migration)
        self.assertIn("'👥 Свободные водители'", migration)

    def test_selection_still_uses_only_free_candidates(self):
        handlers = read("bot/handlers.py")
        candidates = function_source(handlers, "_direct_driver_candidates")
        listing = function_source(handlers, "show_direct_driver_choices")
        self.assertIn("queue_service.free_drivers(session)", candidates)
        self.assertNotIn("format_rating(driver)", listing)


class DriverQueueV99(unittest.TestCase):
    def test_queue_is_numbered_and_shows_ratings(self):
        handlers = read("bot/handlers.py")
        queue = function_source(handlers, "show_queue")
        renderer = function_source(handlers, "_render_driver_statuses")
        self.assertIn("numbered=True", queue)
        self.assertIn("show_rating=True", queue)
        self.assertIn("format_rating(driver)", renderer)
        self.assertIn('line = f"{number}. {line}"', renderer)

    def test_passenger_list_stays_unnumbered(self):
        handlers = read("bot/handlers.py")
        passenger = function_source(handlers, "show_free_drivers")
        self.assertIn("_render_passenger_driver_statuses(items)", passenger)
        self.assertNotIn("numbered=True", passenger)
        self.assertNotIn("show_rating=True", passenger)

    def test_only_dispatcher_queue_exposes_passenger_and_order_number(self):
        queue = function_source(read("bot/handlers.py"), "show_queue")
        renderer = function_source(read("bot/handlers.py"), "_render_driver_statuses")
        self.assertIn("if dispatcher_view else {}", queue)
        self.assertIn("show_passenger_details=dispatcher_view", queue)
        self.assertIn("if show_passenger_details:", renderer)
        self.assertIn('f"заявка #{order_id}{route_suffix}{rating}"', renderer)

    def test_release_uses_new_image_tag(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()
