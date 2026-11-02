# -*- coding: utf-8 -*-
"""V164: safer startup and visible queue-notification failures."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


class SafeDeployV164(unittest.TestCase):
    def test_postgres_migrations_use_cross_process_lock(self):
        source = read("common/db_migrate.py")
        ast.parse(source)
        self.assertIn("pg_advisory_lock", source)
        self.assertIn("pg_advisory_unlock", source)
        self.assertIn("with _database_migration_lock():", source)

    def test_queue_notice_errors_are_not_silent(self):
        source = read("bot/queue_service.py")
        self.assertIn('log = get_logger("bot.queue")', source)
        self.assertIn("Queue-front notification refresh failed", source)
        self.assertIn("Failed to clear queue-front notice", source)

    def test_release_and_github_guide_are_current(self):
        self.assertIn("image: vk-taxi-app:v180", read("docker-compose.yml"))
        guide = read("README_GITHUB_PORTAINER.md")
        self.assertIn("GitHub Actions", guide)
        self.assertIn("резервную копию PostgreSQL", guide)


if __name__ == "__main__":
    unittest.main()