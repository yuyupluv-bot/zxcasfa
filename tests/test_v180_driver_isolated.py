"""Run actual ORM/API tests separately from legacy source-test module stubs."""
import subprocess
import sys
import unittest
from pathlib import Path
class DriverV180Tests(unittest.TestCase):
    def test_actual_orm_and_mobile_api(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,str(root/'tests/integration_v180_driver.py')],cwd=root,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
