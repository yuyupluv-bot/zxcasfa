import ast
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class RatingRepairV54Tests(unittest.TestCase):
    def test_startup_recalculates_both_aggregate_directions(self):
        source=(ROOT/"common/db_migrate.py").read_text("utf-8")
        ast.parse(source, filename="common/db_migrate.py")
        self.assertIn("UPDATE users u SET rating_sum=COALESCE", source)
        self.assertIn("UPDATE users u SET passenger_rating_sum=COALESCE", source)
if __name__=="__main__": unittest.main()
