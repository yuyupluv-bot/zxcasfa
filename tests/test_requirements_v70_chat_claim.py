# -*- coding: utf-8 -*-
"""V70: a claim in the requests chat must never fail silently."""
import io
import os
import unittest
from pathlib import Path

BOT = str(Path(__file__).resolve().parents[1])


def read(path):
    return io.open(os.path.join(BOT, path), encoding="utf-8").read()


class ChatClaimV70(unittest.TestCase):
    def test_chat_reply_helper_posts_into_requests_chat(self):
        src = read("bot/handlers.py")
        self.assertIn("def _chat_claim_reply(", src)
        start = src.index("def _chat_claim_reply(")
        body = src[start:start + 900]
        self.assertIn("order_service.send_fallback_chat_notice(", body)

    def test_stale_offer_lock_is_released_before_a_chat_claim(self):
        src = read("bot/handlers.py")
        self.assertIn("queue_service.offer_lock_is_live(session, user)", src)
        start = src.index("allowed_conversation_cmds = (")
        block = src[start:start + 1600]
        self.assertIn("pending_offer.offered_driver_id = None", block)
        self.assertIn("pending_offer = None", block)

    def test_queue_service_exposes_live_offer_lock(self):
        src = read("bot/queue_service.py")
        self.assertIn("def offer_lock_is_live(", src)
        start = src.index("def offer_lock_is_live(")
        body = src[start:src.index("def release_offer(")]
        self.assertIn('entry.status == "offered"', body)

    def test_every_refusal_is_reported_in_the_chat(self):
        src = read("bot/handlers.py")
        start = src.index("def driver_take_from_chat(")
        body = src[start:start + 4200]
        self.assertGreaterEqual(body.count("_chat_claim_reply("), 3)
        self.assertIn("Chat claim rejected:", body)
        self.assertIn("Chat claim with incomplete car", body)

    def test_taken_order_names_the_driver_who_owns_it(self):
        src = read("bot/handlers.py")
        start = src.index("def driver_take_from_chat(")
        body = src[start:start + 4200]
        self.assertIn("holder = session.get(User, order.driver_id)", body)
        self.assertIn("\u0443\u0436\u0435 \u0432\u0437\u044f\u043b \u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c", body)


if __name__ == "__main__":
    unittest.main()
