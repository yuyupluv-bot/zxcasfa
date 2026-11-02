# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V137(unittest.TestCase):
    def test_all_unanswered_offers_remove_driver_and_use_30_or_60_seconds(self):
        source = (ROOT / "bot/order_service.py").read_text(encoding="utf-8")
        body = source.split("def _accept_timeout", 1)[1].split("def _prearrival_notice", 1)[0]
        self.assertIn("queue_service.leave_queue(session, driver)", body)
        self.assertIn("driver.is_on_line = False", body)
        self.assertIn("30", body)
        self.assertIn("get_int(session, \"driver_accept_timeout\", 60)", body)
        self.assertIn("Вас сняли с линии", body)
        self.assertIn("kb.driver_menu(on_line=False", body)


if __name__ == "__main__":
    unittest.main()
