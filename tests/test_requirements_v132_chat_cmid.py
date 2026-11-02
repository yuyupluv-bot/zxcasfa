from __future__ import annotations
import ast, pathlib, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
def src(p,n):
 s=(ROOT/p).read_text("utf-8"); node=next(x for x in ast.walk(ast.parse(s)) if isinstance(x,ast.FunctionDef) and x.name==n); return ast.get_source_segment(s,node) or ""
class V132(unittest.TestCase):
 def test_events_observed(self): self.assertIn("vk.observe_chat_message(message)",src("bot/main.py","process_event"))
 def test_lookup_read_only(self):
  b=src("bot/vk_client.py","_find_chat_cmid_in_range"); self.assertIn("getByConversationMessageId",b); self.assertNotIn("messages.edit",b)
 def test_exact_match(self): self.assertIn("normalized_chat_text",src("bot/vk_client.py","_find_chat_cmid_in_range"))
 def test_finalize_recovers(self):
  b=src("bot/outbox_service.py","_finalize_one"); self.assertIn("find_existing_chat_cmid",b); self.assertIn("vk_cmid:{cmid}",b)
 def test_backoff(self): self.assertIn("min(300, 2 ** min(row.attempts, 8))",src("bot/outbox_service.py","_finalize_one"))
 def test_original_card_finalized(self): self.assertIn("Заявку #{order.number} взял водитель",src("bot/order_service.py","finalize_chat_order_notice"))
if __name__=="__main__": unittest.main()
