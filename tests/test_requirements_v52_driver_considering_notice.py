import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DriverConsideringNoticeV52Tests(unittest.TestCase):
    def test_passenger_is_notified_after_driver_offer_is_queued(self):
        source = (ROOT / "bot/order_service.py").read_text("utf-8")
        ast.parse(source)
        notice = source.split("def notify_driver_found", 1)[1].split("def clear_offer_notice", 1)[0]
        self.assertNotIn("send_tracked_message", notice)
        self.assertNotIn("Водитель взял вашу заявку", notice)
        self.assertIn("cancel_or_delete", notice)

if __name__ == "__main__": unittest.main()
