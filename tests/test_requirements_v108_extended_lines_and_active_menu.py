# -*- coding: utf-8 -*-
"""V108: Chusovoy/Sarany routes, direction display and stable ride menu."""
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source, name):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


def runtime(rel, functions, assignments):
    source = read(rel)
    tree = ast.parse(source)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in assignments for target in targets):
                nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re, "Order": object}
    exec(compile(module, rel, "exec", flags=__import__("__future__").annotations.compiler_flag,
                 dont_inherit=True), namespace)
    return namespace


class ExtendedLinesV108(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = read("bot/lines.py")
        cls.parallel = read("bot/parallel_orders.py")
        cls.handlers = read("bot/handlers.py")
        cls.keyboards = read("bot/keyboards.py")

    def test_lines_are_seeded_on_existing_database(self):
        self.assertIn('"Чусовой"', self.lines)
        self.assertIn('"Сараны"', self.lines)
        migration = read("common/db_migrate.py")
        self.assertIn("SELECT 'Чусовой', TRUE WHERE NOT EXISTS", migration)
        self.assertIn("SELECT 'Сараны', TRUE WHERE NOT EXISTS", migration)

    def test_new_line_buttons_share_final_row_above_back(self):
        source = function_source(self.keyboards, "lines_keyboard")
        self.assertIn('paired_names = {"чусовой", "сараны"}', source)
        self.assertLess(source.index("if paired:"), source.index('rows.append([_btn("⬅️ Назад"'))

    def test_chusovoy_sarany_and_warm_mountain_origins(self):
        ns = runtime(
            "bot/lines.py",
            {"split_route_segments", "_route_line_in", "_route_line_first_two", "route_origin_line"},
            {"ROUTE_LINE_PATTERNS"},
        )
        origin = ns["route_origin_line"]
        self.assertEqual("Чусовой", origin("Чусовой Ленина 1"))
        self.assertEqual("Чусовой", origin("Нужно Чусовой Ленина 1"))
        self.assertEqual("Чусовой", origin("Можно машину из Чусового"))
        self.assertEqual("Сараны", origin("с Саранов до города"))
        self.assertEqual("Сараны", origin("из Теплой горы до города"))
        self.assertEqual("Сараны", origin("Тёплая гора - Горнозаводск"))
        self.assertIsNone(origin("Горнозаводск - Тёплая гора"))

    def test_dash_route_is_split_into_pickup_and_destination(self):
        ns = runtime("bot/lines.py", {"split_route_segments"}, set())
        self.assertEqual(
            ("Автошкола", "Пашия"),
            ns["split_route_segments"]("Автошкола - Пашия"),
        )
        order_input = function_source(self.handlers, "order_set_addresses")
        self.assertIn("route_origin, route_destination = lines.split_route_segments(raw)", order_input)
        self.assertIn('draft["address_from"] = route_origin or pickup_city or raw', order_input)
        self.assertIn("destination = route_destination or destination or raw", order_input)

    def test_parallel_routes_match_new_destinations(self):
        functions = {
            "_route_city_in", "_route_dash_parts", "_city_first_two",
            "_destination_city", "_origin_city", "_city_before_to_separator",
            "route_priority_city", "_has_return_intent", "_destination_restricted_orders",
        }
        ns = runtime("bot/parallel_orders.py", functions, {"ROUTE_CITY_PATTERNS"})
        row = lambda text: SimpleNamespace(route_text=text, address_from=text, address_to=text)
        self.assertEqual("Чусовой", ns["route_priority_city"](row("из Чусового до города")))
        self.assertEqual("Сараны", ns["route_priority_city"](row("Теплая гора - город")))
        self.assertEqual("Чусовой", ns["_destination_city"]("Автошкола - Чусовой"))
        self.assertEqual("Сараны", ns["_destination_city"]("Город - Тёплая гора"))
        chus = row("Чусовой - Горнозаводск")
        sarany = row("из Саранов до города")
        self.assertEqual([chus], ns["_destination_restricted_orders"](
            row("Автошкола - Чусовой"), [sarany, chus]
        ))
        self.assertEqual([sarany], ns["_destination_restricted_orders"](
            row("Город - Тёплая гора"), [chus, sarany]
        ))

    def test_queue_display_supports_all_requested_localities(self):
        functions = {
            "_without_station_pashiya", "_display_city_in",
            "_display_origin_city", "_display_route_direction",
        }
        ns = runtime(
            "bot/handlers.py", functions,
            {"_DISPLAY_CITY_PATTERNS"},
        )
        direction = ns["_display_route_direction"]
        row = lambda text: SimpleNamespace(route_text=text, address_from=text, address_to=text)
        self.assertEqual(("Койва", None), direction(row("из Койвы в город")))
        self.assertEqual((None, "Подхоз"), direction(row("Автошкола - Подхоз")))
        self.assertEqual(("Дачи", None), direction(row("с Дач в город")))
        self.assertEqual((None, "Чусовой"), direction(row("Автошкола - Чусовой")))
        self.assertEqual(("Тёплая гора", None), direction(row("Тёплая гора - город")))
        for label in (
            "из Чусового", "до Чусового", "из Саранов", "до Саранов",
            "из Тёплой горы", "до Тёплой горы", "из Койвы", "до Подхоза", "до Дач",
        ):
            self.assertIn(label, self.handlers)

    def test_parallel_button_never_says_my_parallel_order(self):
        source = function_source(self.keyboards, "driver_ride_keyboard")
        self.assertIn("🔀 Параллельные заявки", source)
        self.assertNotIn("Моя параллельная заявка", source)

    def test_dispatcher_row_keeps_passenger_and_route_direction(self):
        namespace = {
            "_DISPLAY_ORIGIN_LABELS": {"Чусовой": "из Чусового"},
            "_DISPLAY_DESTINATION_LABELS": {"Чусовой": "до Чусового"},
            "_display_route_direction": lambda _order: (None, "Чусовой"),
            "format_rating": lambda _driver: "5.0",
        }
        exec(
            compile(
                function_source(self.handlers, "_render_driver_statuses"),
                "handlers.py", "exec",
                flags=__import__("__future__").annotations.compiler_flag,
                dont_inherit=True,
            ),
            namespace,
        )
        driver = SimpleNamespace(id=1, vk_id=7, full_name="Водитель", current_line="Чусовой")
        lines = namespace["_render_driver_statuses"]([{
            "driver": driver,
            "status": "busy",
            "position": 1,
            "passenger_label": "[id9|Пассажир]",
            "order_id": 55,
            "order": object(),
        }], show_passenger_details=True)
        self.assertIn("[id9|Пассажир], заявка #55 — до Чусового", lines[0])

    def test_accept_immediately_sets_stable_active_menu(self):
        accept = function_source(self.handlers, "driver_accept")
        restore = function_source(self.handlers, "_restore_active_menu_after_stale_offer")
        self.assertIn("keyboard=_driver_ride_kb(session, order)", accept)
        self.assertLess(
            accept.index("keyboard=_driver_ride_kb(session, order)"),
            accept.index("_show_eta_menu(session, user, order)"),
        )
        self.assertIn("order.driver_id == user.id", restore)
        self.assertIn("Активное меню восстановлено", restore)
        for name in ("driver_accept", "driver_decline_back", "driver_decline_prompt", "driver_decline"):
            self.assertIn("_restore_active_menu_after_stale_offer", function_source(self.handlers, name))

    def test_eta_keeps_canonical_active_menu(self):
        eta = function_source(self.handlers, "_apply_eta")
        self.assertIn("keyboard=_driver_ride_kb(session, order)", eta)


if __name__ == "__main__":
    unittest.main()
