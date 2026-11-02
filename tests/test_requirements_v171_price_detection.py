# -*- coding: utf-8 -*-
"""V171: numbers below 150 stay in the address instead of becoming fares."""
import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "bot" / "customer_price_service.py"
SERVICE = SERVICE_PATH.read_text(encoding="utf-8")


def load_parser():
    tree = ast.parse(SERVICE)
    wanted = {
        "_amount", "parse_entered_price", "extract_customer_price",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(name in names for name in (
                "MIN_PRICE", "MAX_PRICE", "_NUMBER", "_CURRENCY",
                "_TAGGED_RE", "_BARE_END_RE",
            )):
                body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            body.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVICE_PATH), "exec"), namespace)
    return namespace


class PriceDetectionV171(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = staticmethod(load_parser()["extract_customer_price"])

    def test_numbers_below_150_remain_in_request(self):
        for text in (
            "Ленина 12",
            "Мира 7 квартира 45",
            "Советская 149",
            "дом 120 р.",
        ):
            self.assertEqual((text, None), self.parser(text))

    def test_150_through_50000_are_detected_as_price(self):
        self.assertEqual(("Ленина 12", 150), self.parser("Ленина 12 150"))
        self.assertEqual(("Мира 7", 200), self.parser("Мира 7, 200 рублей"))
        self.assertEqual(("Советская 10", 50000), self.parser("Советская 10 50000"))

    def test_free_form_order_does_not_reject_small_numbers(self):
        handlers = (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")
        self.assertNotIn("has_low_customer_price(raw)", handlers)
        self.assertNotIn("Минимальная цена — 150 рублей.", handlers)
        self.assertNotIn("Минимальная цена — 150 рублей.", SERVICE)

    def test_release_image_is_v171(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: vk-taxi-app:v180", compose)


if __name__ == "__main__":
    unittest.main()