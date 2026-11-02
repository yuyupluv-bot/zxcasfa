# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class V145(unittest.TestCase):
 def test_button_below_reviews(self):
  s=(ROOT/'bot/keyboards.py').read_text();b=s.split('def driver_info_menu',1)[1].split('def missed_offer_timeout_keyboard',1)[0]
  self.assertLess(b.index('"reviews"'),b.index('"connect_driver_app"'))
 def test_code_is_one_time_ten_minutes_and_hashed(self):
  s=(ROOT/'bot/handlers.py').read_text();b=s.split('def show_driver_app_pair_code',1)[1].split('def show_my_reviews',1)[0]
  self.assertIn('sha256',b);self.assertIn('minutes=10',b);self.assertIn('randbelow(1_000_000)',b)
 def test_app_registers_only_by_code(self):
  s=(ROOT/'web/push_app.py').read_text();b=s.split('def register',1)[1]
  self.assertIn('payload.get("code")',b);self.assertNotIn('payload.get("vk_id")',b);self.assertNotIn('registration_key',b)
if __name__=='__main__': unittest.main()
