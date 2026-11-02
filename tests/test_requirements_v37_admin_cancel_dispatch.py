import ast
import unittest
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROOT = BOT_ROOT


class AdminCancelDispatchV37Tests(unittest.TestCase):
    def src(self, root, rel):
        return (root / rel).read_text("utf-8")

    def test_changed_modules_parse(self):
        for root, rel in (
            (BOT_ROOT, "bot/admin_order_cancel_service.py"),
            (BOT_ROOT, "bot/outbox_service.py"),
            (ADMIN_ROOT, "bot/admin_order_cancel_service.py"),
            (ADMIN_ROOT, "bot/outbox_service.py"),
            (ADMIN_ROOT, "web/app.py"),
        ):
            ast.parse(self.src(root, rel), filename=str(root / rel))

    def test_admin_route_uses_atomic_cancel_service(self):
        source = self.src(ADMIN_ROOT, "web/app.py")
        route = source.split("def cancel_order(order_id):", 1)[1].split(
            "#  Broadcast", 1
        )[0]
        self.assertIn("with_for_update()", route)
        self.assertIn("from bot.admin_order_cancel_service import cancel_from_admin", route)
        self.assertIn("result = cancel_from_admin(s, order, reason)", route)

    def test_cancellation_stops_every_dispatch_source(self):
        source = self.src(BOT_ROOT, "bot/admin_order_cancel_service.py")
        self.assertIn('order.status = "cancelled"', source)
        self.assertIn('order.cancelled_by = "admin"', source)
        self.assertIn("ScheduledJob.object_id == order.id", source)
        self.assertIn("PassengerQueue.order_id == order.id", source)
        self.assertIn("order.offered_driver_id = None", source)
        self.assertIn("order.parallel_driver_id = None", source)

    def test_pending_and_sent_cards_are_cancelled(self):
        source = self.src(BOT_ROOT, "bot/admin_order_cancel_service.py")
        self.assertIn("_belongs_to_order", source)
        self.assertIn("outbox_service.cancel_or_delete", source)
        self.assertIn('"sending", "sent", "cancel_requested"', source)
        self.assertIn("parallel_notified_driver_ids", source)

    def test_driver_and_passenger_get_final_notice(self):
        source = self.src(BOT_ROOT, "bot/admin_order_cancel_service.py")
        self.assertIn("Ваша заявка #{order.number} отменена администратором", source)
        self.assertIn("Заявка #{order.number} отменена администратором", source)
        self.assertIn("kb.driver_menu", source)
        self.assertIn("kb.passenger_menu", source)

    def test_outbox_refuses_stale_order_card(self):
        source = self.src(BOT_ROOT, "bot/outbox_service.py")
        self.assertIn("def _cancel_if_order_is_stale", source)
        self.assertIn('order.status in ("cancelled", "completed")', source)
        self.assertIn("if _cancel_if_order_is_stale(session, row):", source)
        self.assertIn("suppressed stale order card", source)


if __name__ == "__main__":
    unittest.main()
