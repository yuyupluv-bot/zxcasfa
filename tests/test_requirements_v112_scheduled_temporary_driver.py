# -*- coding: utf-8 -*-
"""V112: schedule the «Суточники» role for an exact local datetime."""
import ast
import datetime as dt
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
def read(rel): return (ROOT / rel).read_text(encoding="utf-8")

class ScheduledTemporaryDriverV112(unittest.TestCase):
    def test_parser_accepts_requested_date_and_time(self):
        source = read("bot/handlers.py")
        wanted = {"_scheduled_local_datetime", "_parse_scheduled_temporary_driver_request"}
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        tz = dt.timezone(dt.timedelta(hours=5))
        namespace = {"re": re, "dt": dt, "time_utils": SimpleNamespace(LOCAL_TZ=tz, now=lambda: dt.datetime.now(tz))}
        tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(tree, "handlers.py", "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
        parse = namespace["_parse_scheduled_temporary_driver_request"]
        now = dt.datetime(2026, 8, 19, 23, 0, tzinfo=tz)
        ref, starts, days = parse("https://vk.ru/id123 20.08 в 12 00", now=now)
        self.assertEqual("https://vk.ru/id123", ref)
        self.assertEqual(dt.datetime(2026, 8, 20, 12, 0, tzinfo=tz), starts)
        self.assertEqual(1, days)
        self.assertEqual(3, parse("@driver 20.08 12:00 на 3 дня", now=now)[2])

    def test_worker_activates_and_later_expires_role(self):
        service = read("bot/temporary_driver_service.py")
        self.assertIn("def activate_driver_if_due", service)
        self.assertIn("driver.grant_role(ROLE_DRIVER)", service)
        self.assertIn("User.temporary_driver_starts_at <= now", service)
        self.assertIn("driver.revoke_role(ROLE_DRIVER)", service)

    def test_schema_and_migration_are_present(self):
        self.assertIn("temporary_driver_starts_at = Column(DateTime(timezone=True))", read("common/models.py"))
        migration = read("migrations/versions/0044_scheduled_temporary_driver.py")
        self.assertIn('down_revision = "0043_direct_driver_selection"', migration)
        self.assertIn("temporary_driver_starts_at TIMESTAMPTZ", migration)

    def test_admin_prompt_contains_natural_example(self):
        handlers = read("bot/handlers.py")
        self.assertIn("20.08 12:00", handlers)
        self.assertIn("20.08 в 12 00 на 3 дня", handlers)

if __name__ == "__main__": unittest.main()
