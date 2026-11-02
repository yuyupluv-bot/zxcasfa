# -*- coding: utf-8 -*-
"""V166: driver application settings, background guidance and store hardening."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class DriverAppStoreReadyV166(unittest.TestCase):
    def test_user_controls_are_present(self):
        layout = read("android-driver-app/app/src/main/res/layout/activity_main.xml")
        for value in (
            "volumeSeek", "vibrationSwitch", "orderSwitch",
            "parallelSwitch", "cancelSwitch", "themeLight", "themeDark",
            "batterySettingsButton", "disconnectButton",
        ):
            self.assertIn(value, layout)
        menu = read("android-driver-app/app/src/main/res/menu/drawer_menu.xml")
        self.assertIn("navMain", menu)
        self.assertIn("navSettings", menu)

    def test_push_service_honours_local_scenarios(self):
        push = read(
            "android-driver-app/app/src/main/java/ru/taxi/driveralerts/PushService.kt"
        )
        self.assertIn("AppPrefs.isScenarioEnabled", push)
        self.assertIn("ContextCompat.startForegroundService", push)

    def test_alarm_does_not_force_system_volume(self):
        alarm = read(
            "android-driver-app/app/src/main/java/ru/taxi/driveralerts/AlarmService.kt"
        )
        self.assertIn("AppPrefs.VOLUME", alarm)
        self.assertNotIn("setStreamVolume", alarm)
        self.assertIn("setWakeMode", alarm)

    def test_manifest_uses_only_needed_capabilities(self):
        manifest = read("android-driver-app/app/src/main/AndroidManifest.xml")
        self.assertIn("android.permission.POST_NOTIFICATIONS", manifest)
        self.assertIn("android.permission.WAKE_LOCK", manifest)
        self.assertIn('android:usesCleartextTraffic="false"', manifest)
        self.assertNotIn("REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", manifest)
        # V180 maps require foreground GPS; no always-on/background tracking.
        self.assertIn("ACCESS_FINE_LOCATION", manifest)
        self.assertNotIn("ACCESS_BACKGROUND_LOCATION", manifest)

    def test_authenticated_device_removal_exists(self):
        server = read("web/push_app.py")
        self.assertIn('/api/driver-app/unregister', server)
        self.assertIn("compare_digest", server)
        self.assertIn("request.db.delete(row)", server)


if __name__ == "__main__":
    unittest.main()