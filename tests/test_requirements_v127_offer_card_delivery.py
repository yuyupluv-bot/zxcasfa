# -*- coding: utf-8 -*-
"""V127: public order numbers must not suppress live driver offer cards."""
import ast
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def function_source(source, name):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)

class OfferCardDeliveryV127(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "bot/outbox_service.py").read_text(encoding="utf-8")
        code = "from __future__ import annotations\n" + function_source(source, "_order_action_ids")
        cls.namespace = {"json": json, "re": re, "_ORDER_ACTION_COMMANDS": {"accept", "decline"}}
        exec(compile(code, "offer_card_ids", "exec"), cls.namespace)

    def test_payload_internal_id_wins_over_visible_public_number(self):
        keyboard = json.dumps({"buttons": [[{"action": {"payload": json.dumps({"cmd": "accept", "order_id": 4682})}}]]})
        row = SimpleNamespace(keyboard=keyboard, text="🔔 Новая заявка #1 (Обычная)")
        self.assertEqual({4682}, self.namespace["_order_action_ids"](row))

    def test_legacy_text_only_card_still_supported(self):
        row = SimpleNamespace(keyboard=None, text="🔔 Новая заявка #99 (Обычная)")
        self.assertEqual({99}, self.namespace["_order_action_ids"](row))

if __name__ == "__main__": unittest.main()
