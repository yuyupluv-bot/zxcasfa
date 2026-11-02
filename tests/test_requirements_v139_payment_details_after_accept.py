# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V139(unittest.TestCase):
    def test_payment_button_is_available_from_assigned_stage(self):
        keyboards = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        assigned = keyboards.split('if stage == "assigned":', 1)[1].split('elif stage == "arrived":', 1)[0]
        self.assertIn('"cmd": "send_payment_details"', assigned)

    def test_payment_action_accepts_assigned_order(self):
        handlers = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        body = handlers.split("def driver_send_payment_details", 1)[1].split("def ", 1)[0]
        self.assertIn('("assigned", "arrived", "in_progress")', body)


if __name__ == "__main__":
    unittest.main()
