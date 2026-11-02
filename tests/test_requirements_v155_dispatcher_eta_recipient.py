# -*- coding: utf-8 -*-
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def function_source(path: Path, name: str) -> str:
    source = path.read_text("utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


class DispatcherEtaRecipientV155(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.block = function_source(ROOT / "bot/handlers.py", "_apply_eta")

    def test_eta_is_only_accepted_from_assigned_driver(self):
        self.assertIn("assigned_driver.id != user.id", self.block)
        self.assertIn("Ignored ETA from non-assigned user", self.block)

    def test_dispatcher_receives_driver_car_number_and_eta(self):
        self.assertIn("dispatcher_driver_details(assigned_driver)", self.block)
        self.assertIn("Прибудет примерно через: {eta} мин.", self.block)

    def test_driver_only_confirmation_goes_to_assigned_driver(self):
        self.assertIn(
            'vk.send_message(assigned_driver.vk_id, "✅ Время сохранено.',
            self.block,
        )
        self.assertNotIn(
            'vk.send_message(user.vk_id, "✅ Время сохранено.',
            self.block,
        )


if __name__ == "__main__":
    unittest.main()
