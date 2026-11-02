"""Keep real ORM tests separate from legacy tests that stub common.models."""
import subprocess
import sys
import unittest
from pathlib import Path

class RequestedFixesV181(unittest.TestCase):
    def test_real_orm_requested_scenarios(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,str(root/'tests/integration_v181_fixes.py')],cwd=root,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
