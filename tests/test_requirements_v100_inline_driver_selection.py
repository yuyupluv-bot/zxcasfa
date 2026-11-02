# -*- coding: utf-8 -*-
"""V100: unchanged passenger menu and inline selection for up to 30 drivers."""
import ast
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class InlineDriverSelectionV100(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_models = types.ModuleType("common.models")
        fake_models.ROLE_ADMIN = "admin"
        fake_models.ROLE_DISPATCHER = "dispatcher"
        fake_models.ROLE_DRIVER = "driver"
        fake_models.ROLE_PASSENGER = "passenger"
        sys.modules.setdefault("common.models", fake_models)
        from bot import keyboards
        cls.kb = keyboards
        cls.handlers = read("bot/handlers.py")

    def test_01_drivers_view_keeps_main_menu_and_state(self):
        view = function_source(self.handlers, "show_free_drivers")
        self.assertIn("kb.passenger_menu(", view)
        self.assertNotIn("set_state(", view)
        self.assertNotIn("free_drivers_entry_keyboard", view)

    def test_02_selector_supports_thirty_inline_buttons(self):
        drivers = [
            SimpleNamespace(id=index, vk_id=1000 + index, full_name=f"Водитель {index}", current_line="Горнозаводск")
            for index in range(1, 31)
        ]
        raw = json.loads(self.kb.free_driver_choices_keyboard(drivers))
        self.assertTrue(raw["inline"])
        self.assertEqual(6, len(raw["buttons"]))
        self.assertTrue(all(len(row) == 5 for row in raw["buttons"]))
        labels = [button["action"]["label"] for row in raw["buttons"] for button in row]
        self.assertEqual([f"Водитель {index}" for index in range(1, 31)], labels)
        payloads = [json.loads(button["action"]["payload"]) for row in raw["buttons"] for button in row]
        self.assertEqual(list(range(1, 31)), [payload["driver_id"] for payload in payloads])

    def test_03_names_are_listed_in_message(self):
        listing = function_source(self.handlers, "show_direct_driver_choices")
        self.assertIn("drivers[:DIRECT_DRIVER_PAGE_SIZE]", listing)
        self.assertIn("for driver in visible", listing)
        self.assertIn('f"• {name} — линия: {line_name}"', listing)
        self.assertNotIn("format_rating(driver)", listing)
        self.assertIn("Нажмите кнопку с именем", listing)
        self.assertIn("Показаны первые 30", listing)

    def test_04_bottom_menu_has_only_refresh_and_main_menu(self):
        raw = json.loads(self.kb.direct_driver_controls_keyboard())
        self.assertFalse(raw["inline"])
        labels = [button["action"]["label"] for row in raw["buttons"] for button in row]
        self.assertEqual(["🔄 Обновить список", "⬅️ Вернуться в главное меню"], labels)
        commands = [json.loads(button["action"]["payload"])["cmd"] for row in raw["buttons"] for button in row]
        self.assertEqual(["driver_select_list", "start"], commands)

    def test_05_release_uses_current_image(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()
