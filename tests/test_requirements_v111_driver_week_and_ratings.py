# -*- coding: utf-8 -*-
"""V111: weekly driver completions and review leaderboard."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def read(rel): return (ROOT / rel).read_text(encoding="utf-8")
def function_source(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)

class DriverWeekAndRatingsV111(unittest.TestCase):
    def test_week_stats_cover_seven_days(self):
        source = read("web/statistics_service.py")
        fn = function_source(source, "build_driver_week_stats")
        self.assertIn("range(6, -1, -1)", fn)
        self.assertIn("[0] * 7", fn)
        self.assertIn("range(7)", fn)

    def test_driver_page_queries_last_week(self):
        fn = function_source(read("web/app.py"), "drivers_view")
        self.assertIn("dt.timedelta(days=6)", fn)
        self.assertIn('Order.status == "completed"', fn)
        self.assertIn("build_driver_week_stats", fn)

    def test_all_drivers_are_sorted_by_review_count(self):
        fn = function_source(read("web/app.py"), "drivers_view")
        self.assertIn('User.granted_roles.like("%driver%")', fn)
        self.assertIn("-int(driver.rating_count or 0)", fn)
        self.assertIn("-float(driver.rating or 0)", fn)

    def test_template_shows_week_and_rating_leaderboard(self):
        template = read("web/templates/drivers.html")
        self.assertIn("Выполнено за неделю", template)
        self.assertIn("Все водители и отзывы", template)
        self.assertIn("driver.rating_count", template)
        self.assertIn("driver.rating", template)
        self.assertIn("rating_rows", template)

if __name__ == "__main__": unittest.main()
