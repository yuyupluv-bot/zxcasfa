# -*- coding: utf-8 -*-
"""V102: inline driver buttons use names and builds no longer require tag bumps."""
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


class NamedDriverButtonsV102(unittest.TestCase):
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

    @staticmethod
    def drivers(count):
        return [
            SimpleNamespace(id=index, vk_id=1000 + index, full_name=f"Водитель {index}", current_line="Пашия")
            for index in range(1, count + 1)
        ]

    def test_01_buttons_show_names_instead_of_numbers(self):
        raw = json.loads(self.kb.free_driver_choices_keyboard(self.drivers(30)))
        labels = [button["action"]["label"] for row in raw["buttons"] for button in row]
        self.assertEqual([f"Водитель {index}" for index in range(1, 31)], labels)
        self.assertFalse(any(label.isdigit() for label in labels))

    def test_02_layout_adapts_up_to_thirty_names(self):
        for count in (1, 6, 7, 12, 13, 18, 19, 24, 25, 30):
            raw = json.loads(self.kb.free_driver_choices_keyboard(self.drivers(count)))
            self.assertTrue(raw["inline"])
            self.assertLessEqual(len(raw["buttons"]), 6)
            self.assertTrue(all(1 <= len(row) <= 5 for row in raw["buttons"]))
            self.assertEqual(count, sum(len(row) for row in raw["buttons"]))

    def test_03_message_prompts_for_driver_name(self):
        listing = function_source(read("bot/handlers.py"), "show_direct_driver_choices")
        self.assertIn("Нажмите кнопку с именем", listing)
        self.assertNotIn("Нажмите кнопку с номером", listing)
        self.assertIn('f"• {name} — линия: {line_name}"', listing)

    def test_04_compose_forces_build_without_future_tag_changes(self):
        compose = read("docker-compose.yml")
        self.assertIn("image: vk-taxi-app:v180", compose)
        self.assertIn("pull_policy: build", compose)


if __name__ == "__main__":
    unittest.main()
