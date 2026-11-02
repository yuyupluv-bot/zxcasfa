# -*- coding: utf-8 -*-
"""V167: public privacy policy and revocable RuStore review access."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class RuStoreReviewV167(unittest.TestCase):
    def test_privacy_page_is_public_and_complete(self):
        server = read("web/push_app.py")
        page = read("web/templates/privacy.html")
        self.assertIn('@bp.route("/privacy")', server)
        self.assertIn("Перунова Анастасия Анатольевна", page)
        self.assertIn("ilia.gat@yandex.ru", page)
        self.assertIn("panel.bezrateadminps.ru", page)
        self.assertNotIn("extends", page)

    def test_review_code_is_reusable_revocable_and_hashed(self):
        server = read("web/push_app.py")
        self.assertIn("dt.timedelta(days=30)", server)
        self.assertIn("is_reusable=True", server)
        self.assertIn('label="RuStore moderation"', server)
        self.assertIn("sha256(code.encode", server)
        self.assertIn('action=="revoke_review_code"', server)
        self.assertIn("if not pairing.is_reusable: pairing.used_at=now", server)

    def test_schema_migration_exists(self):
        model = read("common/models.py")
        migration = read("migrations/versions/0052_rustore_review_codes.py")
        self.assertIn("is_reusable = Column(Boolean", model)
        self.assertIn('revision = "0052_rustore_review_codes"', migration)
        self.assertIn('down_revision = "0051_outbox_dont_parse_links"', migration)

    def test_android_accepts_normal_and_review_codes(self):
        activity = read(
            "android-driver-app/app/src/main/java/ru/taxi/driveralerts/MainActivity.kt"
        )
        layout = read("android-driver-app/app/src/main/res/layout/activity_main.xml")
        self.assertIn("value.length !in 6..10", activity)
        self.assertIn('android:maxLength="10"', layout)
        self.assertIn("https://panel.bezrateadminps.ru/privacy",
                      read("android-driver-app/gradle.properties"))

    def test_admin_template_has_csrf_for_every_form(self):
        template = read("web/templates/driver_app.html")
        self.assertEqual(template.count("<form "), template.count('name="csrf_token"'))
        self.assertIn('value="create_review_code"', template)
        self.assertIn('value="revoke_review_code"', template)
        self.assertIn('value="save_scenarios"', template)


if __name__ == "__main__":
    unittest.main()