import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LongDistanceBookingV35Tests(unittest.TestCase):
    def src(self, rel):
        return (ROOT / rel).read_text("utf-8")

    def test_changed_files_parse(self):
        for rel in (
            "bot/handlers.py", "bot/keyboards.py", "common/settings_service.py",
            "common/db_migrate.py", "migrations/versions/0036_long_distance_booking_only.py",
        ):
            ast.parse(self.src(rel), filename=rel)

    def test_passenger_button_has_new_name(self):
        settings = self.src("common/settings_service.py")
        keyboards = self.src("bot/keyboards.py")
        self.assertIn('"btn_booking": "📅 Бронь дальние поездки"', settings)
        self.assertIn('labels.get("btn_booking", "📅 Бронь дальние поездки")', keyboards)

    def test_passenger_skips_type_choice(self):
        handlers = self.src("bot/handlers.py")
        fill = handlers.split("def passenger_booking_fill", 1)[1].split(
            "def passenger_booking_type", 1
        )[0]
        self.assertIn('draft = {"type": "far_distance"}', fill)
        self.assertIn("States.P_BOOKING_DATE", fill)
        self.assertIn("На какое число бронируем дальнюю поездку?", fill)
        self.assertNotIn("booking_type_keyboard", fill)
        self.assertNotIn("Выберите тип брони", fill)

    def test_old_type_buttons_also_open_far_distance(self):
        handlers = self.src("bot/handlers.py")
        compatibility = handlers.split("def passenger_booking_type", 1)[1].split(
            "def passenger_booking_date_quick", 1
        )[0]
        self.assertIn("return passenger_booking_fill(session, user)", compatibility)
        self.assertNotIn('booking_type == "early_time"', compatibility)

    def test_dispatcher_main_button_is_direct(self):
        keyboards = self.src("bot/keyboards.py")
        menu = keyboards.split("def dispatcher_menu", 1)[1].split(
            "def dispatcher_booking_menu", 1
        )[0]
        self.assertIn('"📅 Бронь дальней поездки"', menu)
        self.assertIn('{"cmd": "disp_booking_new"}', menu)
        self.assertNotIn('{"cmd": "disp_booking_menu"}', menu)
        self.assertIn('"🗓 Мои брони"', menu)

    def test_dispatcher_and_passenger_share_same_start(self):
        handlers = self.src("bot/handlers.py")
        dispatcher = handlers.split("def handle_dispatcher", 1)[1]
        self.assertIn('if cmd == "disp_booking_new":', dispatcher)
        self.assertIn("return passenger_booking_start(session, user)", dispatcher)

    def test_existing_database_caption_is_migrated(self):
        migration = self.src("migrations/versions/0036_long_distance_booking_only.py")
        self.assertIn('down_revision = "0035_interactive_prompt_cleanup"', migration)
        self.assertIn("Бронь дальние поездки", migration)
        self.assertIn("btn_booking", self.src("common/db_migrate.py"))


if __name__ == "__main__":
    unittest.main()
