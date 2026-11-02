# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V142(unittest.TestCase):
    def test_post_ride_menu_has_away_action(self):
        source = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        body = source.split("def post_ride_line_keyboard", 1)[1].split("def pending_orders_keyboard", 1)[0]
        self.assertIn("Отлучиться", body)
        self.assertIn('{"cmd": "driver_away"}', body)


if __name__ == "__main__":
    unittest.main()
