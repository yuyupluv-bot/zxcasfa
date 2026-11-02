import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InteractiveCleanupV34Tests(unittest.TestCase):
    def src(self, rel):
        return (ROOT / rel).read_text("utf-8")

    def test_changed_python_parses(self):
        for rel in (
            "bot/handlers.py", "bot/keyboards.py", "bot/passenger_queue.py",
            "bot/order_service.py", "bot/timers.py", "common/models.py",
            "common/config.py", "common/db_migrate.py",
            "migrations/versions/0035_interactive_prompt_cleanup.py",
        ):
            ast.parse(self.src(rel), filename=rel)

    def test_rating_cards_are_tracked_and_finalized(self):
        handlers = self.src("bot/handlers.py")
        self.assertIn("passenger_rating_prompt_outbox_id = vk.send_tracked_message", handlers)
        self.assertIn("driver_rating_prompt_outbox_id = vk.send_tracked_message", handlers)
        self.assertIn("def _finish_rating_prompt", handlers)
        self.assertIn("outbox_service.finalize_tracked_message", handlers)
        keyboards = self.src("bot/keyboards.py")
        self.assertIn('{"cmd": "skip_rate", "order_id": order_id}', keyboards)
        self.assertIn('{"cmd": "skip_rate_passenger", "order_id": order_id}', keyboards)

    def test_departure_answer_removes_old_choice(self):
        handlers = self.src("bot/handlers.py")
        self.assertIn('return passenger_departure_response(', handlers)
        self.assertIn('cancel=True, order_id=payload.get("order_id")', handlers)
        self.assertNotIn('"✅ Вы ответили: Да, жду."', handlers)
        self.assertIn('session, prompt_id, final_text, replace_text=True, defer_edit=True', handlers)
        self.assertIn("replacement_keyboard='{\"buttons\":[],\"inline\":true}'", handlers)

    def test_queue_actuality_card_is_tracked(self):
        queue = self.src("bot/passenger_queue.py")
        self.assertIn("entry.actuality_prompt_outbox_id = vk.send_tracked_message", queue)
        self.assertIn("def _clear_actuality_prompt", queue)
        self.assertIn("def pause_actuality_prompts", queue)
        self.assertIn("delete=True", queue)

    def test_actuality_timeout_is_five_minutes_and_has_exact_text(self):
        self.assertIn('PASSENGER_POLL_TIMEOUT: int = int(_get("PASSENGER_POLL_TIMEOUT", "60"))', self.src("common/config.py"))
        queue = self.src("bot/passenger_queue.py")
        self.assertIn('get_int(session, "passenger_poll_timeout", 60)', queue)
        exact = "Вы не подтвердили актуальность заявки, ваша заявка отменена автоматически"
        self.assertIn(exact, queue)
        self.assertIn(exact, self.src("bot/handlers.py"))

    def test_chat_actuality_has_persistent_five_minute_timeout(self):
        handlers = self.src("bot/handlers.py")
        self.assertIn('timers.schedule("chat_actual", order.id, 60', handlers)
        self.assertIn("def chat_actuality_timeout", handlers)
        timers = self.src("bot/timers.py")
        self.assertIn('"chat_actual"', timers)
        self.assertIn("chat_actuality_timeout(object_id)", timers)

    def test_driver_reservation_pauses_other_actuality_questions(self):
        service = self.src("bot/order_service.py")
        self.assertIn("passenger_queue.pause_actuality_prompts(session, except_order_id=order.id)", service)
        queue = self.src("bot/passenger_queue.py")
        self.assertIn("if active_polls:", queue)
        self.assertIn("pause_actuality_prompts(session, except_order_id=order.id)", queue)

    def test_migration_and_raw_guards_include_prompt_columns(self):
        migration = self.src("migrations/versions/0035_interactive_prompt_cleanup.py")
        self.assertIn('down_revision = "0034_temporary_driver_until"', migration)
        self.assertIn("actuality_prompt_outbox_id", migration)
        self.assertIn("passenger_rating_prompt_outbox_id", migration)
        guard = self.src("common/db_migrate.py")
        self.assertIn("UPDATE settings SET value='60'", guard)


if __name__ == "__main__":
    unittest.main()
