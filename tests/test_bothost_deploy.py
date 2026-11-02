"""Deployment-only tests: no production credentials or VK calls."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import bothost_main as launcher


class BothostDeployTests(unittest.TestCase):
    def env(self):
        return dict(DATABASE_URL='postgresql://test:test@127.0.0.1/test', VK_TOKEN='test-token',
                    VK_GROUP_ID='123', SECRET_KEY='a'*64, ADMIN_LOGIN='admin', ADMIN_PASSWORD='local-test-only')

    def test_valid_environment(self):
        self.assertEqual(launcher.validate_environment(self.env()), [])

    def test_missing_configuration_never_leaks_values(self):
        env = self.env(); env['DATABASE_URL'] = 'INVALID_SECRET_DB_URL'; env['PORT'] = '70000'
        env['VK_GROUP_ID'] = 'bad'; env['SECRET_KEY'] = 'TOPSECRET'; env['ADMIN_PASSWORD'] = ''
        errors = '\n'.join(launcher.validate_environment(env))
        for name in ('DATABASE_URL', 'PORT', 'VK_GROUP_ID', 'SECRET_KEY', 'ADMIN_PASSWORD'):
            self.assertIn(name, errors)
        for secret in ('TOPSECRET', 'INVALID_SECRET_DB_URL'):
            self.assertNotIn(secret, errors)

    def test_postgres_required(self):
        env = self.env(); env['DATABASE_URL'] = 'sqlite:///data/db.sqlite'
        self.assertTrue(any('DATABASE_URL' in e for e in launcher.validate_environment(env)))

    def test_role_specific_sql_limits_and_no_repeat_migrations(self):
        with patch.dict(os.environ, {'BOT_DB_STATEMENT_TIMEOUT_MS':'1234', 'ADMIN_DB_STATEMENT_TIMEOUT_MS':'5678'}):
            self.assertEqual(launcher.child_environment('bot')['DB_STATEMENT_TIMEOUT_MS'], '1234')
            self.assertEqual(launcher.child_environment('admin')['DB_STATEMENT_TIMEOUT_MS'], '5678')
            self.assertEqual(launcher.child_environment('bot')['AUTO_MIGRATE_ON_START'], '0')

    def test_real_child_crash_restart_and_sibling_survival(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory)/'pids.txt'
            script = ("import os,time; from pathlib import Path; "
                      f"p=Path({str(record)!r}); "
                      "p.open('a').write(str(os.getpid())+'\\n'); "
                      "time.sleep(0.2)" )
            stop = threading.Event()
            timer = threading.Timer(3, stop.set); timer.start()
            began = time.monotonic()
            try:
                result = launcher.supervise(stop, {
                    'bot':[sys.executable,'-c',script],
                    'admin':[sys.executable,'-c','import time; time.sleep(60)'],
                })
            finally:
                stop.set(); timer.cancel()
            self.assertEqual(result, 0)
            pids = record.read_text().splitlines()
            self.assertGreaterEqual(len(pids), 2)
            self.assertLess(time.monotonic()-began, 8)
            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid), 0)

    def test_container_and_archive_entrypoints(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('"bothost_main.py"', (root/'Dockerfile').read_text())
        self.assertIn('"bothost_main.py"', (root/'bothost.json').read_text())


if __name__ == '__main__':
    unittest.main()
