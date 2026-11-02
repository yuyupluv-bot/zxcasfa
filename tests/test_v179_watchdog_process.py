"""Exercise the watchdog in its own disposable process, never the test runner."""
import subprocess
import sys
import unittest
from pathlib import Path

class ProcessRecoveryTest(unittest.TestCase):
    def test_stalled_child_dumps_threads_and_exits_nonzero(self):
        code='''
import time
from bot import runtime_guard as guard
original_sleep = time.sleep
# Accelerate ONLY this throwaway process; production delays are unchanged.
guard.time.sleep = lambda seconds: original_sleep(0.01)
guard.start()
with guard._lock:
    guard._startup_at = time.monotonic() - 1000
original_sleep(2)
raise SystemExit(99)
'''
        p=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],
                         capture_output=True,text=True,timeout=5,
                         env={**__import__('os').environ,'BOT_WATCHDOG_ENABLED':'1'})
        self.assertEqual(p.returncode,70,p.stderr)
        self.assertIn('startup exceeded deadline',p.stderr)
        self.assertIn('Thread',p.stderr)
        self.assertIn('Docker restart policy',p.stderr)

if __name__=='__main__':unittest.main()
