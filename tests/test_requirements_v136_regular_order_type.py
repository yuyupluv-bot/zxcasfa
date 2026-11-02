# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V136(unittest.TestCase):
    def test_new_route_resets_stale_delivery_type_before_detection(self):
        source = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        block = source.split("def order_set_addresses", 1)[1].split("def order_set_voice", 1)[0]
        reset = block.index('draft["order_type"] = "regular"')
        partner = block.index("is_partner_delivery_text(raw)")
        delivery = block.index("is_delivery_text(raw)")
        self.assertLess(reset, partner)
        self.assertLess(reset, delivery)


if __name__ == "__main__":
    unittest.main()
