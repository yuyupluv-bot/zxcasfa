import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PreserveBottomMenuV36Tests(unittest.TestCase):
    def test_outbox_module_parses(self):
        source = (ROOT / "bot/outbox_service.py").read_text("utf-8")
        ast.parse(source, filename="bot/outbox_service.py")

    def test_inline_card_uses_inline_empty_keyboard(self):
        source = (ROOT / "bot/outbox_service.py").read_text("utf-8")
        fn = source.split("def finalize_tracked_message", 1)[1].split(
            "def _claim_finalize_batch", 1
        )[0]
        self.assertIn("compact_keyboard", fn)
        self.assertIn("'\"inline\":true' in compact_keyboard", fn)
        self.assertIn("'{\"buttons\":[],\"inline\":true}'", fn)

    def test_regular_empty_keyboard_remains_for_regular_cards(self):
        source = (ROOT / "bot/outbox_service.py").read_text("utf-8")
        self.assertIn("empty_keyboard = '{\"buttons\":[],\"one_time\":true}'", source)

    def test_rating_cards_are_inline(self):
        source = (ROOT / "bot/keyboards.py").read_text("utf-8")
        rating = source.split("def rating_keyboard", 1)[1].split(
            "def passenger_rating_keyboard", 1
        )[0]
        driver_rating = source.split("def passenger_rating_keyboard", 1)[1].split(
            "def skip_keyboard", 1
        )[0]
        self.assertIn("inline=True", rating)
        self.assertIn("inline=True", driver_rating)


if __name__ == "__main__":
    unittest.main()
