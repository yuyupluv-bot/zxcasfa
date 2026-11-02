# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AwayMovesToTailV151(unittest.TestCase):
    def test_away_driver_remains_visible_in_queue(self):
        source = (ROOT / "bot/queue_service.py").read_text("utf-8")
        block = source.split("def set_away", 1)[1].split("def return_from_away", 1)[0]
        self.assertIn('entry.status = "away"', block)
        self.assertNotIn("session.delete(entry)", block)

    def test_return_from_away_moves_driver_to_tail(self):
        source = (ROOT / "bot/queue_service.py").read_text("utf-8")
        block = source.split("def return_from_away", 1)[1].split("def mark_assigned", 1)[0]
        self.assertIn('entry.status = "waiting"', block)
        self.assertIn("_move_to_tail(session, entry)", block)

    def test_away_status_is_rendered_in_queue(self):
        source = (ROOT / "bot/handlers.py").read_text("utf-8")
        self.assertIn('status = "Отлучился"', source)


if __name__ == "__main__":
    unittest.main()
