"""V117: current parallel notices and a protected active driver menu."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text("utf-8")


class ParallelMenuNoticesV117(unittest.TestCase):
    def test_parallel_notice_is_tracked_on_driver_and_reconciled(self):
        models = read("common/models.py")
        parallel = read("bot/parallel_orders.py")
        self.assertIn("parallel_notice_outbox_id = Column(Integer)", models)
        self.assertIn("parallel_notice_count = Column(Integer, default=0, nullable=False)", models)
        self.assertIn("def _sync_parallel_notice", parallel)
        self.assertIn("outbox_service.cancel_or_delete(session, outbox_id)", parallel)
        self.assertIn("def start_notice_worker", parallel)
        self.assertIn("AGGREGATE_NOTICE_POLL_SECONDS = 5.0", parallel)

    def test_active_ride_keyboard_is_protected_from_outbox_cap(self):
        client = read("bot/vk_client.py")
        self.assertIn("def _keyboard_priority", client)
        self.assertIn("return 2_000", client)
        self.assertIn('\"cmd\":\"driver_cancel_active\"', client)
        self.assertIn("max(\n                    self._message_priority(clean_text)", client)

    def test_worker_and_migration_are_wired(self):
        self.assertIn("parallel_orders.start_notice_worker()", read("bot/main.py"))
        migration = read("migrations/versions/0045_parallel_notice_tracking.py")
        self.assertIn('down_revision = "0044_scheduled_temporary_driver"', migration)
        self.assertIn("parallel_notice_outbox_id", migration)
        self.assertIn("parallel_notice_count", migration)
