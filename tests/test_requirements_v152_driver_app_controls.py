# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DriverAppControlsV152(unittest.TestCase):
    def test_bot_receives_firebase_secret_mount(self):
        compose = (ROOT / "docker-compose.yml").read_text("utf-8")
        bot = compose.split("  bot:", 1)[1].split("  admin:", 1)[0]
        self.assertIn("./secrets:/app/secrets:ro", bot)

    def test_pairing_screen_has_no_skip_or_return_buttons(self):
        layout = (ROOT / "android-driver-app/app/src/main/res/layout/activity_main.xml").read_text("utf-8")
        self.assertNotIn('@+id/skip', layout)
        self.assertNotIn('@+id/backToPair', layout)
        self.assertIn('@+id/toggleNotifications', layout)

    def test_push_is_suppressed_when_locally_disabled(self):
        source = (ROOT / "android-driver-app/app/src/main/java/ru/taxi/driveralerts/PushService.kt").read_text("utf-8")
        self.assertIn("AppPrefs.NOTIFICATIONS", source)
        self.assertIn("AppPrefs.PAIRED", source)

    def test_push_devices_are_filtered_by_target_driver(self):
        source = (ROOT / "bot/push_service.py").read_text("utf-8")
        self.assertIn("driver_id=int(driver_id)", source)
        self.assertIn("is_active=True", source)

    def test_public_apk_download_route_exists(self):
        source = (ROOT / "web/push_app.py").read_text("utf-8")
        self.assertIn('/driver-app/download.apk', source)
        self.assertIn('application/vnd.android.package-archive', source)


if __name__ == "__main__":
    unittest.main()
