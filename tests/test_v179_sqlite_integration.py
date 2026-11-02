"""Run real-ORM scenarios in fresh processes: old source tests stub common.models."""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

@unittest.skipUnless(importlib.util.find_spec('sqlalchemy') is not None,'Install pinned requirements for ORM checks')
class IsolatedOrmTests(unittest.TestCase):
    def run_case(self, name):
        root=Path(__file__).resolve().parents[1]
        p=subprocess.run([sys.executable,str(root/'tests/integration_v179_orm.py'),
                          'RealOrmTests.'+name],cwd=root,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_expiry_commits_server_decision_and_queues_ui_edit(self):
        self.run_case('test_expiry_commits_server_decision_and_queues_ui_edit')

    def test_real_row_flood_backoff_and_terminal_permission(self):
        self.run_case('test_real_row_flood_backoff_and_terminal_permission')

    def test_active_send_is_excluded_from_cleanup_claims(self):
        self.run_case('test_active_send_is_excluded_from_cleanup_claims')

if __name__=='__main__':unittest.main()
