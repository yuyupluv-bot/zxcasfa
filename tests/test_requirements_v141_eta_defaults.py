# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V141(unittest.TestCase):
    def test_eta_values_are_mandatory_and_ignore_old_persisted_default(self):
        source = (ROOT / "bot/handlers.py").read_text(encoding="utf-8")
        body = source.split("def _eta_options", 1)[1].split("def _show_eta_menu", 1)[0]
        self.assertIn("return [1, 3, 5, 10, 15, 25]", body)
        self.assertNotIn("get_cached", body)


if __name__ == "__main__":
    unittest.main()
