# -*- coding: utf-8 -*-
"""V114: dedicated temporary-driver admin page and transactional notices."""
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
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {name}")


class TemporaryDriversAdminV114(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = read("web/app.py")
        cls.base = read("web/templates/base.html")
        cls.page = read("web/templates/temporary_drivers.html")

    def test_01_navigation_hides_logs_and_places_temporary_drivers_after_drivers(self):
        self.assertNotIn("('logs','Логи'", self.base)
        drivers = self.base.index("('drivers_view','Водители'")
        temporary = self.base.index("('temporary_drivers_view','Суточники'")
        orders = self.base.index("('orders','Заказы'")
        self.assertLess(drivers, temporary)
        self.assertLess(temporary, orders)

    def test_02_all_four_admin_actions_and_routes_exist(self):
        expected = {
            "temporary_drivers_view": '/temporary-drivers',
            "create_temporary_driver": '/temporary-drivers/create',
            "extend_temporary_driver": '/temporary-drivers/<int:user_id>/extend',
            "cancel_scheduled_temporary_driver": '/temporary-drivers/<int:user_id>/cancel',
            "remove_temporary_driver": '/temporary-drivers/<int:user_id>/remove',
        }
        for name, route in expected.items():
            self.assertIn(f'@app.route("{route}', self.app)
            function_source(self.app, name)
        for label in ("Добавить суточника", "Продлить", "Отменить выдачу", "Снять суточника"):
            self.assertIn(label, self.page)

    def test_03_every_form_has_csrf_and_destructive_actions_confirm(self):
        self.assertEqual(self.page.count("<form "), self.page.count('name="csrf_token"'))
        self.assertGreaterEqual(self.page.count("<form "), 4)
        self.assertIn("confirm('Отменить запланированную выдачу роли?')", self.page)
        self.assertIn("confirm('Снять временную роль водителя?')", self.page)

    def test_04_every_change_queues_a_notice_without_direct_vk_send(self):
        helper = function_source(self.app, "_queue_admin_user_notice")
        self.assertIn("OutboxMessage(", helper)
        self.assertIn('status="pending"', helper)
        self.assertIn("next_attempt_at=time_utils.now()", helper)
        for name in (
            "create_temporary_driver",
            "extend_temporary_driver",
            "cancel_scheduled_temporary_driver",
            "remove_temporary_driver",
        ):
            body = function_source(self.app, name)
            self.assertIn("_queue_admin_user_notice", body)
            self.assertNotIn("vk.send_message", body)
            self.assertNotIn("requests.post", body)

    def test_05_add_supports_immediate_and_scheduled_issue(self):
        body = function_source(self.app, "create_temporary_driver")
        self.assertIn("_resolve_vk_user", body)
        resolver = function_source(self.app, "_resolve_vk_user")
        self.assertIn("(?:com|ru)", resolver)
        self.assertIn("_parse_local_admin_datetime", body)
        self.assertIn("user.grant_role(ROLE_DRIVER)", body)
        self.assertIn("user.temporary_driver_starts_at = starts_at", body)
        self.assertIn("user.temporary_driver_until", body)
        self.assertIn("if user.has_role(ROLE_DRIVER):", body)
        self.assertIn("У пользователя уже есть постоянная роль водителя", body)

    def test_06_extend_and_cancel_update_the_saved_schedule(self):
        extend = function_source(self.app, "extend_temporary_driver")
        self.assertIn("max(deadline, now) + dt.timedelta(days=days)", extend)
        self.assertIn("Новое окончание", extend)
        cancel = function_source(self.app, "cancel_scheduled_temporary_driver")
        self.assertIn("user.temporary_driver_starts_at = None", cancel)
        self.assertIn("user.temporary_driver_until = None", cancel)
        self.assertIn("отменил запланированную", cancel)

    def test_07_removal_is_safe_for_queue_selection_and_active_trip(self):
        route = function_source(self.app, "remove_temporary_driver")
        self.assertIn("_temporary_driver_active_work", route)
        self.assertIn("user.temporary_driver_until = time_utils.now()", route)
        self.assertIn("роль будет снята сразу после её завершения", route)
        cleanup = function_source(self.app, "_remove_temporary_driver_now")
        for requirement in (
            "DriverSelection", "DriverQueue", "cancel_or_delete",
            "s.delete(selection)", "s.delete(entry)",
            "user.is_on_line = False", 'user.driver_status = "offline"',
            "user.revoke_role(ROLE_DRIVER)",
            "user.temporary_driver_starts_at = None",
            "user.temporary_driver_until = None",
        ):
            self.assertIn(requirement, cleanup)

    def test_08_internal_audit_is_kept_while_log_tab_is_hidden(self):
        self.assertIn('def logs():', self.app)
        self.assertIn("AdminLog", self.app)
        for action in (
            "create_temporary_driver", "schedule_temporary_driver",
            "extend_temporary_driver", "cancel_scheduled_temporary_driver",
            "remove_temporary_driver",
        ):
            self.assertIn(action, self.app)

    def test_09_layout_has_desktop_and_mobile_rules(self):
        css = read("web/static/css/style.css")
        self.assertIn("/* Temporary drivers admin */", css)
        self.assertIn(".temp-create-form", css)
        self.assertIn(".temp-drivers-table", css)
        self.assertIn("@media(max-width:768px)", css)

    def test_10_release_metadata_is_v114(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))
        self.assertIn("VK Taxi V167", read("README_GITHUB_PORTAINER.md"))
        self.assertTrue((ROOT / "CHANGELOG_V114.md").is_file())


if __name__ == "__main__":
    unittest.main()
