# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V135(unittest.TestCase):
    def test_special_requests_use_first_offer_and_chat_fallback(self):
        source = (ROOT / "bot/order_service.py").read_text(encoding="utf-8")
        self.assertIn("30\n        if order.customer_price", source)
        self.assertIn("if order.customer_price is not None or order.dispatcher_id", source)
        self.assertIn("publish_direct_to_requests_chat(session, order)", source)
        self.assertIn("f\"@all\\n🔔 Заявка №{order.number}\\n\"", source)

    def test_chat_claim_assigns_and_opens_eta_flow(self):
        source = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        block = source.split("def driver_take_from_chat", 1)[1]
        self.assertIn("order.status = \"assigned\"", block)
        self.assertIn("_show_eta_menu(", block)

    def test_unanswered_special_offer_removes_driver_from_line(self):
        source = (ROOT / "bot/order_service.py").read_text(encoding="utf-8")
        block = source.split("def _accept_timeout", 1)[1].split("def _prearrival_notice", 1)[0]
        self.assertIn("queue_service.leave_queue(session, driver)", block)
        self.assertIn("driver.is_on_line = False", block)
        self.assertIn("Вас сняли с линии", block)


if __name__ == "__main__":
    unittest.main()
