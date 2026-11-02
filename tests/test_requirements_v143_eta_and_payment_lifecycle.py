# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V143(unittest.TestCase):
    def test_every_standard_eta_uses_required_values(self):
        handlers = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        body = handlers.split("def _eta_options", 1)[1].split("def _show_eta_menu", 1)[0]
        self.assertIn("return [1, 3, 5, 10, 15, 25]", body)

    def test_delivery_and_parallel_eta_include_one_and_three(self):
        source = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        delivery = source.split("def delivery_eta_keyboard", 1)[1].split("def eta_add_keyboard", 1)[0]
        parallel = source.split("def parallel_eta_keyboard", 1)[1].split("def ", 1)[0]
        self.assertIn("[1, 3, 5, 10, 15, 25]", delivery)
        self.assertIn('"minutes": 1', parallel)
        self.assertIn('"minutes": 3', parallel)
        self.assertIn('"minutes": 25', parallel)

    def test_payment_button_lives_until_sent_or_completed(self):
        source = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        body = source.split("def driver_ride_keyboard", 1)[1].split("def driver_arrived_keyboard", 1)[0]
        self.assertGreaterEqual(body.count("payment_details_enabled and not payment_details_sent"), 3)
        self.assertIn('stage == "assigned"', body)
        self.assertIn('stage == "arrived"', body)
        self.assertIn('stage == "in_progress"', body)


if __name__ == "__main__":
    unittest.main()
