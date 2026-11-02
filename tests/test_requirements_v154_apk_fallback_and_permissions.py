# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ApkFallbackAndPermissionsV154(unittest.TestCase):
    def test_apk_uploaded_under_android_project_is_served(self):
        source = (ROOT / "web/push_app.py").read_text("utf-8")
        self.assertIn(
            '/app/android-driver-app/app/downloads/BEZTAKSY.apk',
            source,
        )
        self.assertIn('def _find_apk_file', source)

    def test_push_sound_volume_permissions_are_initialized(self):
        compose = (ROOT / "docker-compose.yml").read_text("utf-8")
        self.assertIn('push-sounds-init:', compose)
        self.assertIn('chown -R 999:999 /data/push_sounds', compose)
        admin = compose.split('  admin:', 1)[1]
        self.assertIn('push-sounds-init:', admin)
        self.assertIn('condition: service_completed_successfully', admin)


if __name__ == "__main__":
    unittest.main()
