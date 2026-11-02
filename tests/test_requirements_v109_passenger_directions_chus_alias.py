# -*- coding: utf-8 -*-
"""V109: passenger directions and colloquial Чус/Чуса aliases."""
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


def runtime(rel, functions, assignments):
    source = read(rel)
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in assignments for target in targets):
                nodes.append(node)
    tree = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(tree)
    namespace = {"re": re, "Order": object}
    exec(compile(
        tree, rel, "exec",
        flags=__import__("__future__").annotations.compiler_flag,
        dont_inherit=True,
    ), namespace)
    return namespace


class PassengerDirectionsChusAliasV109(unittest.TestCase):
    def test_chus_aliases_are_chusovoy_pickups(self):
        ns = runtime(
            "bot/lines.py",
            {"split_route_segments", "_route_line_in", "_route_line_first_two", "route_origin_line"},
            {"ROUTE_LINE_PATTERNS"},
        )
        origin = ns["route_origin_line"]
        self.assertEqual("Чусовой", origin("чус вокзал"))
        self.assertEqual("Чусовой", origin("машина чуса центр"))
        self.assertEqual("Чусовой", origin("из чуса до города"))
        self.assertEqual("Чусовой", origin("чус - Горнозаводск"))
        self.assertIsNone(origin("Автошкола - чус"))

    def test_chus_aliases_work_for_parallel_origin_and_destination(self):
        ns = runtime(
            "bot/parallel_orders.py",
            {
                "_route_city_in", "_route_dash_parts", "_city_first_two",
                "_destination_city", "_origin_city", "_city_before_to_separator",
                "route_priority_city",
            },
            {"ROUTE_CITY_PATTERNS"},
        )
        row = lambda text: SimpleNamespace(route_text=text, address_from=text, address_to=text)
        self.assertEqual("Чусовой", ns["route_priority_city"](row("из чуса до города")))
        self.assertEqual("Чусовой", ns["_destination_city"]("Автошкола - чус"))
        self.assertEqual("Чусовой", ns["_destination_city"]("Автошкола до чуса"))

    def test_warm_mountain_with_e_or_yo_is_sarany(self):
        ns = runtime(
            "bot/lines.py",
            {"split_route_segments", "_route_line_in", "_route_line_first_two", "route_origin_line"},
            {"ROUTE_LINE_PATTERNS"},
        )
        origin = ns["route_origin_line"]
        self.assertEqual("Сараны", origin("Теплая гора - город"))
        self.assertEqual("Сараны", origin("Тёплая гора - город"))
        self.assertEqual("Сараны", origin("из Теплой горы"))
        self.assertEqual("Сараны", origin("из Тёплой горы"))

    def test_passenger_driver_lists_feed_active_routes_to_shared_renderer(self):
        source = read("bot/handlers.py")
        for name in ("_show_driver_status_overview", "show_free_drivers"):
            function = function_source(source, name)
            self.assertIn("active_orders = _active_orders_by_driver", function)
            self.assertIn('"order": active_orders.get(driver.id)', function)
            self.assertIn("_render_passenger_driver_statuses(items)", function)

    def test_passenger_renderer_displays_chus_direction(self):
        source = read("bot/handlers.py")
        ns = runtime(
            "bot/handlers.py",
            {
                "_without_station_pashiya", "_display_city_in",
                "_display_origin_city", "_display_route_direction",
            },
            {"_DISPLAY_CITY_PATTERNS"},
        )
        direction = ns["_display_route_direction"]
        row = lambda text: SimpleNamespace(route_text=text, address_from=text, address_to=text)
        self.assertEqual(("Чусовой", None), direction(row("из чуса до города")))
        self.assertEqual((None, "Чусовой"), direction(row("Автошкола - чус")))
        self.assertIn('"Чусовой": "из Чусового"', source)
        self.assertIn('"Чусовой": "до Чусового"', source)


if __name__ == "__main__":
    unittest.main()
