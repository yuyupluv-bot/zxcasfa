# -*- coding: utf-8 -*-
"""V169: requests mentioning «Кайф» always stay ordinary rides."""
import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DELIVERY_PATH = ROOT / "bot" / "delivery_service.py"
DELIVERY = DELIVERY_PATH.read_text(encoding="utf-8")


def load_matchers():
    tree = ast.parse(DELIVERY)
    functions = {
        "_normalize", "has_delivery_word", "is_partner_text", "is_kaif_text",
        "is_partner_delivery_text", "is_delivery_text",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(name in names for name in (
                "PARTNER_DELIVERY_TYPE", "_DELIVERY_WORD", "_PARTNER_PATTERNS",
            )):
                body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            body.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(DELIVERY_PATH), "exec"), namespace)
    return namespace


class KaifRegularOrderV169(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matchers = load_matchers()

    def test_kaif_delivery_phrases_are_not_deliveries(self):
        for text in (
            "Доставка Кайф на Ленина 5",
            "доставка от кайфа до Мира 7",
            "доставить из Кайфа заказ",
            "Кайф, доставка клиенту",
            "доставка из kaif",
            "kayf доставка",
        ):
            self.assertTrue(self.matchers["is_kaif_text"](text), text)
            self.assertFalse(self.matchers["is_partner_delivery_text"](text), text)
            self.assertFalse(self.matchers["is_delivery_text"](text), text)

    def test_kaif_name_without_delivery_is_also_regular_ride(self):
        for text in (
            "от Кайфа до Ленина 5",
            "Кайф - Мира 7",
            "забрать у кайфа и отвезти домой",
        ):
            self.assertTrue(self.matchers["is_kaif_text"](text), text)
            self.assertFalse(self.matchers["is_partner_text"](text), text)
            self.assertFalse(self.matchers["is_partner_delivery_text"](text), text)
            self.assertFalse(self.matchers["is_delivery_text"](text), text)

    def test_existing_partners_are_unchanged(self):
        for text in (
            "доставка из Красного бархата",
            "Галактика доставка клиенту",
        ):
            self.assertTrue(self.matchers["is_partner_delivery_text"](text), text)

    def test_release_image_is_v169(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: vk-taxi-app:v180", compose)


if __name__ == "__main__":
    unittest.main()