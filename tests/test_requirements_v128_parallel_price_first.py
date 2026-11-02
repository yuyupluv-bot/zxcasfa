# -*- coding: utf-8 -*-
"""V128: customer price is the first item in the driver's parallel list."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def function_source(source, name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


class ParallelPriceFirstV128(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parallel = (ROOT / "bot/parallel_orders.py").read_text(encoding="utf-8")
        cls.keyboards = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        code = "from __future__ import annotations\n" + function_source(
            cls.parallel, "_parallel_list_text"
        )
        cls.namespace = {}
        exec(compile(code, "parallel_list_text", "exec"), cls.namespace)

    def test_price_is_printed_first(self):
        order = SimpleNamespace(
            id=4682,
            number=1,
            customer_price=200,
            route_text="от мотеля по городу",
            address_from=None,
            address_to=None,
        )
        self.assertEqual(
            "200 рублей, #1 — от мотеля по городу",
            self.namespace["_parallel_list_text"](order),
        )

    def test_no_price_keeps_normal_list_format(self):
        order = SimpleNamespace(
            id=4683,
            number=2,
            customer_price=None,
            route_text="вокзал — центр",
            address_from=None,
            address_to=None,
        )
        self.assertEqual(
            "#2 — вокзал — центр",
            self.namespace["_parallel_list_text"](order),
        )

    def test_button_shows_public_number_but_keeps_internal_payload(self):
        self.assertIn('f"Взять заявку #{order_number}"', self.keyboards)
        self.assertIn('{"cmd": "parallel_take", "order_id": order_id}', self.keyboards)
        self.assertIn("choices.append((order.id, order.number, route))", self.parallel)


if __name__ == "__main__":
    unittest.main()
