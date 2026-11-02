# -*- coding: utf-8 -*-
"""V116: both rating keyboards expire in their original message after 12h."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


class RatingExpiryV116(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = read("bot/rating_expiry_service.py")
        cls.handlers = read("bot/handlers.py")
        cls.timers = read("bot/timers.py")
        cls.outbox = read("bot/outbox_service.py")

    def test_01_changed_sources_parse(self):
        for rel in (
            "bot/rating_expiry_service.py",
            "bot/handlers.py",
            "bot/timers.py",
        ):
            ast.parse(read(rel), filename=rel)

    def test_02_deadline_is_exactly_twelve_hours(self):
        self.assertIn("RATING_EXPIRY_SECONDS = 12 * 60 * 60", self.service)
        schedule = function_source(self.service, "schedule_expiry")
        self.assertIn("RATING_EXPIRY_SECONDS", schedule)
        self.assertIn("timers.schedule(", schedule)

    def test_03_expiry_edits_original_card_without_new_message(self):
        expire = function_source(self.service, "expire")
        self.assertIn('EXPIRY_TEXT = "Время для оценки истекло."', self.service)
        self.assertIn("outbox_service.finalize_tracked_message(", expire)
        self.assertIn("int(outbox_id)", expire)
        self.assertNotIn("send_message", self.service)
        finalize = function_source(self.outbox, "finalize_tracked_message")
        self.assertIn('\"buttons\":[],\"inline\":true', finalize)

    def test_04_passenger_and_driver_have_independent_persistent_jobs(self):
        self.assertIn('"passenger_rating_expire"', self.service)
        self.assertIn('"driver_rating_expire"', self.service)
        restore = function_source(self.timers, "_restored_callback")
        self.assertIn('kind in ("passenger_rating_expire", "driver_rating_expire")', restore)
        self.assertIn("driver_side=(kind == \"driver_rating_expire\")", restore)

    def test_05_every_original_rating_prompt_starts_expiry(self):
        self.assertEqual(
            3,
            self.handlers.count("rating_expiry_service.schedule_expiry(order.id"),
        )
        driver_prompt = function_source(self.handlers, "_ask_driver_rate_passenger")
        delivery = function_source(self.handlers, "driver_complete_delivery")
        ride = function_source(self.handlers, "driver_complete_ride")
        self.assertIn("driver_side=True", driver_prompt)
        self.assertIn("driver_side=False", delivery)
        self.assertIn("driver_side=False", ride)

    def test_06_manual_answer_cancels_matching_job(self):
        finish = function_source(self.handlers, "_finish_rating_prompt")
        self.assertIn("rating_expiry_service.cancel_expiry", finish)
        self.assertIn("driver_side=driver_side", finish)

    def test_07_timeout_and_click_are_serialized(self):
        expire = function_source(self.service, "expire")
        self.assertIn(".with_for_update()", expire)
        for name in (
            "passenger_skip_rating",
            "driver_skip_passenger_rating",
            "save_rating",
            "driver_rate_passenger",
        ):
            self.assertIn("with_for_update", function_source(self.handlers, name), name)

    def test_08_stale_buttons_cannot_save_or_skip(self):
        passenger_rate = function_source(self.handlers, "save_rating")
        driver_rate = function_source(self.handlers, "driver_rate_passenger")
        passenger_skip = function_source(self.handlers, "passenger_skip_rating")
        driver_skip = function_source(self.handlers, "driver_skip_passenger_rating")
        self.assertIn("if not order.passenger_rating_prompt_outbox_id", passenger_rate)
        self.assertIn("if not order.driver_rating_prompt_outbox_id", driver_rate)
        self.assertIn("not order.passenger_rating_prompt_outbox_id", passenger_skip)
        self.assertIn("not order.driver_rating_prompt_outbox_id", driver_skip)

    def test_09_upgrade_recovers_old_open_rating_cards(self):
        recover = function_source(self.service, "schedule_missing_for_open_prompts")
        self.assertIn("ScheduledJob.kind.in_", recover)
        self.assertIn("Order.passenger_rating_prompt_outbox_id.isnot(None)", recover)
        self.assertIn("Order.driver_rating_prompt_outbox_id.isnot(None)", recover)
        self.assertIn("RATING_EXPIRY_SECONDS - elapsed", recover)
        self.assertIn("max(1.0, RATING_EXPIRY_SECONDS - elapsed)", recover)
        main = read("bot/main.py")
        self.assertIn("rating_expiry_service.schedule_missing_for_open_prompts()", main)

    def test_10_expiry_clears_only_matching_legacy_rate_state(self):
        expire = function_source(self.service, "expire")
        self.assertIn("state.state == States.P_RATE", expire)
        self.assertIn("state_order_id == order.id", expire)
        self.assertIn("reset(session, passenger.vk_id, States.MAIN_MENU)", expire)


if __name__ == "__main__":
    unittest.main()
