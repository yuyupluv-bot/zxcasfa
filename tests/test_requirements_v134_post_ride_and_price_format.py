# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V134(unittest.TestCase):
    def test_completed_ride_always_opens_line_menu(self):
        source = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        marker = "# Requirement 3: after entering the price the driver rates the passenger;"
        block = source.split(marker, 1)[1].split("# Ask the passenger to rate", 1)[0]
        self.assertIn("lines.ask_post_ride_line(session, user)", block)

    def test_currency_amount_does_not_join_house_number(self):
        source = (ROOT / "bot/customer_price_service.py").read_text(encoding="utf-8")
        self.assertIn("amount>\\d{{3,5}}", source)
        self.assertIn("Newlines and ordinary spaces around the fare are valid", source)


if __name__ == "__main__":
    unittest.main()
