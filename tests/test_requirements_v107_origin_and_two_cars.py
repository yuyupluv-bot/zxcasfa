# -*- coding: utf-8 -*-
"""V107: village-before-«до» origin and two-car FIFO requests."""
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def function_node(source, name):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def function_source(source, name):
    return ast.get_source_segment(source, function_node(source, name)) or ""


class OriginBeforeToV107(unittest.TestCase):
    def test_lines_parser_treats_village_before_to_as_pickup(self):
        source = read("bot/lines.py")
        tree = ast.parse(source)
        nodes = [
            node for node in tree.body
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name)
                    and target.id in {
                        "DEFAULT_LINES", "LINE_ALIASES", "ROUTE_LINE_PATTERNS"
                    }
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            ) or (
                isinstance(node, ast.FunctionDef)
                and node.name in {
                    "_resolve_alias", "_clean_token", "parse_pickup_city",
                    "split_route_segments", "_route_line_in",
                    "_route_line_first_two", "route_origin_line",
                }
            )
        ]
        namespace = {"re": re, "Iterable": Iterable}
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "lines_origin_runtime", "exec"), namespace)
        parse = namespace["parse_pickup_city"]

        city, _ = parse("Можно две машинки от заимки Пашии до города")
        self.assertEqual("Пашия", city)
        city, _ = parse("Можно 2 машины от заимки Кусьи до города")
        self.assertEqual("Кусья", city)
        city, _ = parse("Можно машину из города до Пашии")
        self.assertIsNone(city)
        city, _ = parse("Можно машину из города до Кусьи")
        self.assertIsNone(city)

    def test_dispatch_route_uses_same_origin_rule(self):
        source = read("bot/parallel_orders.py")
        names = {
            "_route_city_in", "_route_dash_parts",
            "_city_first_two", "_origin_city", "_city_before_to_separator",
            "route_priority_city",
        }
        tree = ast.parse(source)
        pattern_assignment = next(
            ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "ROUTE_CITY_PATTERNS"
                for target in node.targets
            )
        )
        code = (
            "from __future__ import annotations\n"
            + pattern_assignment + "\n\n"
            + "\n\n".join(function_source(source, name) for name in names)
        )
        namespace = {"re": re, "Order": object}
        exec(compile(code, "parallel_origin_runtime", "exec"), namespace)
        route_city = namespace["route_priority_city"]
        self.assertEqual(
            "Пашия",
            route_city(SimpleNamespace(
                route_text="Можно две машинки от заимки Пашии до города",
                address_from=None,
            )),
        )
        self.assertEqual(
            "Кусья",
            route_city(SimpleNamespace(
                route_text="Можно 2 машины от заимки Кусьи до города",
                address_from=None,
            )),
        )
        self.assertIsNone(route_city(SimpleNamespace(
            route_text="Можно машину из города до Пашии",
            address_from=None,
        )))


class TwoCarsV107(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.keyboards = read("bot/keyboards.py")
        cls.orders = read("bot/order_service.py")

    def test_requested_two_car_phrases(self):
        tree = ast.parse(self.handlers)
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TWO_CAR_REQUEST_RE"
                for target in node.targets
            ):
                nodes.append(node)
            if isinstance(node, ast.FunctionDef) and node.name == "requested_car_count":
                nodes.append(node)
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"re": re}
        exec(compile(module, "two_car_runtime", "exec"), namespace)
        count = namespace["requested_car_count"]
        for text in (
            "Можно две машинки от заимки Пашии до города",
            "Нужно две машины до вокзала",
            "Можно 2 машины до улицы Ленина",
            "Нужно два автомобиля",
            "Закажите 2 авто",
        ):
            self.assertEqual(2, count(text), text)
        for text in ("Нужна машина", "Одна машинка", "Дом 2, нужна машина"):
            self.assertEqual(1, count(text), text)

    def test_passenger_creates_and_dispatches_two_independent_orders(self):
        create = function_source(self.handlers, "create_passenger_order")
        self.assertIn("for car_index in range(car_count):", create)
        self.assertIn("orders.append(order)", create)
        self.assertIn("for item in orders:", create)
        self.assertIn("passenger_queue.dispatch_new_order(session, item)", create)
        self.assertIn("keyboard=kb.passenger_waiting_keyboard(item.id)", create)

    def test_dispatcher_uses_the_same_two_order_fifo_flow(self):
        create = function_source(self.handlers, "disp_create_order_from_draft")
        self.assertIn("for _car_index in range(car_count):", create)
        self.assertIn("for order in orders:", create)
        self.assertIn("order_service.offer_to_next_driver(session, order)", create)
        self.assertIn("по порядку очереди", create)

    def test_second_dispatch_cannot_reuse_first_reserved_driver(self):
        offer = function_source(self.orders, "offer_to_next_driver")
        self.assertIn("queue_service.mark_offered(session, driver)", offer)
        self.assertIn("DriverQueue.status == \"waiting\"", read("bot/queue_service.py"))

    def test_multi_car_buttons_carry_exact_order_id(self):
        for name in (
            "passenger_waiting_keyboard",
            "passenger_departure_keyboard",
            "passenger_ride_keyboard",
            "passenger_arrived_keyboard",
        ):
            source = function_source(self.keyboards, name)
            self.assertIn("order_id", source, name)
        departure = function_source(self.handlers, "passenger_departure_response")
        self.assertIn("passenger_order_for(session, user, order_id)", departure)

    def test_cancelling_one_car_keeps_the_other_active(self):
        cancel = function_source(self.handlers, "passenger_cancel")
        keep = function_source(
            self.handlers, "_continue_after_passenger_order_closed"
        )
        self.assertIn("_continue_after_passenger_order_closed", cancel)
        self.assertIn("Order.id != closed_order_id", keep)
        self.assertIn("Остальные заявки продолжают выполняться", keep)


if __name__ == "__main__":
    unittest.main()
