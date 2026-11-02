# -*- coding: utf-8 -*-
"""V113: authoritative line departure, complete selector, stable queue."""
from __future__ import annotations

import ast
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(name)


class QueueDepartureSelectionV113(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = read("bot/handlers.py")
        cls.queue = read("bot/queue_service.py")

    def test_01_eight_free_drivers_produce_eight_working_payloads(self):
        source = read("bot/keyboards.py")
        wanted_functions = {"fit_label", "_btn", "keyboard", "free_driver_choices_keyboard"}
        wanted_constants = {
            "BLUE", "WHITE", "VK_INLINE_MAX_ROWS", "VK_REGULAR_MAX_ROWS",
            "VK_INLINE_MAX_BUTTONS_PER_ROW", "VK_REGULAR_MAX_BUTTONS_PER_ROW",
            "VK_MAX_LABEL_LENGTH",
        }
        nodes = []
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
                nodes.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                names = []
                if isinstance(node, ast.Assign):
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                elif isinstance(node.target, ast.Name):
                    names = [node.target.id]
                if any(name in wanted_constants for name in names):
                    nodes.append(node)
        namespace = {"json": json, "math": math}
        tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        future = __import__("__future__").annotations.compiler_flag
        exec(compile(tree, "keyboards.py", "exec", flags=future), namespace)
        drivers = [
            SimpleNamespace(id=index, vk_id=1000 + index, full_name=f"Водитель {index}")
            for index in range(1, 9)
        ]
        raw = json.loads(namespace["free_driver_choices_keyboard"](drivers))
        payloads = [
            json.loads(button["action"]["payload"])
            for row in raw["buttons"] for button in row
        ]
        self.assertEqual(8, len(payloads))
        self.assertEqual(list(range(1, 9)), [payload["driver_id"] for payload in payloads])

    def test_02_departure_deletes_row_and_cancels_front_notice(self):
        leave = function_source(self.queue, "leave_queue")
        self.assertIn("with_for_update()", leave)
        self.assertIn("driver.is_on_line = False", leave)
        self.assertIn('driver.driver_status = "offline"', leave)
        self.assertIn("_clear_front_notice(session, entry)", leave)
        self.assertIn("session.delete(entry)", leave)
        self.assertIn("session.flush()", leave)

    def test_03_stale_offline_rows_are_never_free_or_ranked(self):
        for name in ("free_drivers", "free_drivers_on_line"):
            body = function_source(self.queue, name)
            self.assertIn("driver.is_on_line", body)
            self.assertIn('driver.driver_status == "online"', body)
        for name in ("driver_queue_rank", "driver_line_rank"):
            body = function_source(self.queue, name)
            self.assertIn("not driver.is_on_line", body)
            self.assertIn('driver.driver_status != "online"', body)
        actual = function_source(self.queue, "actual_driver_statuses")
        self.assertIn("not driver.is_on_line", actual)
        self.assertIn('result[driver.id] = "offline"', actual)

    def test_04_duplicate_online_event_preserves_position(self):
        join = function_source(self.queue, "join_queue")
        self.assertIn("already_waiting_here", join)
        self.assertIn("if not already_waiting_here:", join)
        self.assertIn("_move_to_tail(session, entry)", join)

    def test_05_customer_requested_cancel_moves_driver_to_first(self):
        front = function_source(self.queue, "move_to_front")
        self.assertIn("_min_position(session) - 1", front)
        self.assertIn('entry.status = "waiting"', front)
        self.assertIn("driver.is_on_line = True", front)
        self.assertIn('driver.driver_status = "online"', front)
        cancel = function_source(self.handlers, "driver_cancel_active")
        customer = cancel.split('if reason == "customer_cancel":', 1)[1].split('if reason == "no_show":', 1)[0]
        self.assertIn("queue_service.move_to_front(session, user)", customer)
        self.assertIn("1-е место в очереди", customer)
        self.assertIn("elif restored_to_front:", customer)

    def test_06_leave_line_closes_concurrent_direct_selection(self):
        driver_handler = function_source(self.handlers, "handle_driver")
        leave_branch = driver_handler.split('if cmd == "leave_line":', 1)[1].split('if cmd == "stay_line":', 1)[0]
        self.assertEqual(2, leave_branch.count("cancel_direct_selection_for_driver(session, user)"))
        offline = function_source(self.handlers, "driver_go_offline")
        after_leave = offline.split("queue_service.leave_queue(session, user)", 1)[1]
        self.assertIn("cancel_direct_selection_for_driver(session, user)", after_leave)

    def test_07_passenger_free_list_matches_selector_exactly(self):
        for name in ("show_free_drivers", "_show_driver_status_overview"):
            body = function_source(self.handlers, name)
            self.assertIn("_direct_driver_candidates(session)", body)
            self.assertIn("driver.id in selectable_ids", body)
        candidates = function_source(self.handlers, "_direct_driver_candidates")
        self.assertIn("queue_service.free_drivers(session)", candidates)
        self.assertIn("lines.has_complete_car(driver)", candidates)


if __name__ == "__main__":
    unittest.main()
