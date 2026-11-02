# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V146(unittest.TestCase):
    def test_preview_and_sound_toggle_exist(self):
        layout = (ROOT / "android-driver-app/app/src/main/res/layout/activity_main.xml").read_text()
        activity = (ROOT / "android-driver-app/app/src/main/java/ru/taxi/driveralerts/MainActivity.kt").read_text()
        self.assertNotIn('@+id/skip', layout)
        self.assertIn('@+id/test', layout)
        self.assertIn('@+id/toggleNotifications', layout)
        self.assertIn('@+id/dashboard', layout)
        self.assertIn('ОСТАНОВИТЬ ЗВУК', activity)
        self.assertIn('setAction("STOP")', activity)

    def test_alarm_respects_local_volume_without_changing_system(self):
        service = (ROOT / "android-driver-app/app/src/main/java/ru/taxi/driveralerts/AlarmService.kt").read_text()
        self.assertIn('AppPrefs.VOLUME', service)
        self.assertIn('setVolume(volume, volume)', service)
        self.assertNotIn('setStreamVolume', service)
        self.assertIn('VibrationEffect.createWaveform', service)


if __name__ == "__main__":
    unittest.main()
