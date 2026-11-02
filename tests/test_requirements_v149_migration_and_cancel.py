# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V149(unittest.TestCase):
    def test_driver_app_tables_have_real_alembic_migration(self):
        migration = (ROOT / "migrations/versions/0048_driver_app_tables.py").read_text()
        self.assertIn('down_revision = "0047_public_numbers"', migration)
        for table in ("driver_push_devices", "push_scenarios", "driver_app_pair_codes"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", migration)

    def test_cancel_flushes_before_remaining_order_query(self):
        source = (ROOT / "bot/handlers.py").read_text()
        block = source.split("def _continue_after_passenger_order_closed", 1)[1].split("def _driver_card", 1)[0]
        self.assertLess(block.index("session.flush()"), block.index("session.query(Order)"))


if __name__ == "__main__":
    unittest.main()
