# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V140(unittest.TestCase):
    def test_eta_and_payment_buttons_share_one_row(self):
        source = (ROOT / "bot/keyboards.py").read_text(encoding="utf-8")
        body = source.split('if stage == "assigned":', 1)[1].split('elif stage == "arrived":', 1)[0]
        self.assertIn("eta_button", body)
        self.assertIn('"cmd": "send_payment_details"', body)
        self.assertIn("rows.append([", body)
        self.assertIn("eta_button,", body)


if __name__ == "__main__":
    unittest.main()
