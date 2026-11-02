# -*- coding: utf-8 -*-
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StabilizationV156(unittest.TestCase):
    def test_python_sources_parse(self):
        for relative in (
            "bot/admin_order_assign_service.py",
            "common/models.py",
            "common/db_migrate.py",
            "web/app.py",
            "web/auth.py",
            "web/push_app.py",
            "migrations/versions/0049_driver_device_secret.py",
        ):
            ast.parse((ROOT / relative).read_text("utf-8"), filename=relative)

    def test_admin_assignment_uses_one_lifecycle_service(self):
        web = (ROOT / "web/app.py").read_text("utf-8")
        service = (ROOT / "bot/admin_order_assign_service.py").read_text("utf-8")
        block = web.split("def assign_driver(order_id):", 1)[1].split(
            "@app.route", 1
        )[0]
        self.assertIn("assign_from_admin", block)
        self.assertNotIn('order.status = "assigned"', block)
        self.assertIn(".with_for_update()", service)
        self.assertIn("queue_service.mark_assigned", service)
        self.assertIn("ACTIVE_DRIVER_STATUSES", service)

    def test_driver_token_rotation_is_authenticated(self):
        source = (ROOT / "web/push_app.py").read_text("utf-8")
        self.assertIn('"/api/driver-app/token"', source)
        self.assertIn("secrets.compare_digest", source)
        self.assertIn("device_secret_hash", source)
        model = (ROOT / "common/models.py").read_text("utf-8")
        self.assertIn("device_secret_hash = Column(String(64)", model)

    def test_android_honours_push_scenario(self):
        alarm = (
            ROOT
            / "android-driver-app/app/src/main/java/ru/taxi/driveralerts/AlarmService.kt"
        ).read_text("utf-8")
        push = (
            ROOT
            / "android-driver-app/app/src/main/java/ru/taxi/driveralerts/PushService.kt"
        ).read_text("utf-8")
        self.assertIn('getStringExtra("vibrate") != "0"', alarm)
        self.assertIn('getIntExtra("repeat", 5)', alarm)
        self.assertIn("prepareAsync()", alarm)
        self.assertIn("override fun onNewToken", push)

    def test_web_security_is_fail_closed(self):
        source = (ROOT / "web/app.py").read_text("utf-8")
        self.assertIn('raise RuntimeError("SECRET_KEY must be configured")', source)
        self.assertIn('raise RuntimeError("Database schema migration failed")', source)
        self.assertIn("MAX_CONTENT_LENGTH", source)
        self.assertIn("Content-Security-Policy", source)
        self.assertIn("def _validated_admin_setting", source)
        self.assertIn("math.isfinite(parsed)", source)


if __name__ == "__main__":
    unittest.main()