# -*- coding: utf-8 -*-
"""V71: a request claimed in the chat is always assigned to that driver."""
import io
import os
import unittest
from pathlib import Path

BOT = str(Path(__file__).resolve().parents[1])


def read(path):
    return io.open(os.path.join(BOT, path), encoding="utf-8").read()


def claim_body():
    src = read("bot/handlers.py")
    start = src.index("def driver_take_from_chat(")
    end = src.index("def passenger_chat_order_actual(", start)
    return src[start:end]


class ChatClaimAlwaysAssignsV71(unittest.TestCase):
    def test_order_row_is_locked_like_every_other_claim_path(self):
        body = claim_body()
        self.assertIn(".with_for_update().one_or_none()", body)

    def test_only_an_active_ride_refuses_the_claim(self):
        body = claim_body()
        head = body[:body.index("pending_offer = offered_order_for(")]
        self.assertIn("active = active_order_for(session, user, as_driver=True)", head)
        self.assertIn("if active and active.id != order.id:", head)
        # Missing car details must not stop the claim any more.
        self.assertNotIn("require_complete_car", body)

    def test_away_or_offline_driver_is_put_back_on_line(self):
        body = claim_body()
        self.assertIn('if was_offline or user.driver_status == "away":', body)
        self.assertIn("queue_service.join_queue(", body)

    def test_pending_offer_is_handed_to_the_next_driver(self):
        body = claim_body()
        self.assertIn("pending_offer.offered_driver_id = None", body)
        self.assertIn("queue_service.release_offer(session, user)", body)
        assign = body.index("queue_service.mark_assigned(session, user)")
        reoffer = body.index("order_service.offer_to_next_driver(session, pending_offer)")
        self.assertLess(assign, reoffer)

    def test_chat_button_is_not_blocked_by_a_pending_offer(self):
        src = read("bot/handlers.py")
        start = src.index("allowed_conversation_cmds = (")
        block = src[start:start + 1800]
        self.assertIn('if pending_offer and cmd != "chat_take":', block)

    def test_assignment_is_flushed_before_any_notification(self):
        body = claim_body()
        assign = body.index('order.status = "assigned"')
        flush = body.index("session.flush()", assign)
        notice = body.index("order_service.finalize_chat_order_notice(")
        self.assertLess(flush, notice)
        tail = body[flush:]
        self.assertIn("except Exception as exc:  # noqa: BLE001", tail)

    def test_closed_statuses_are_explicit(self):
        src = read("bot/handlers.py")
        self.assertIn("CHAT_CLAIM_CLOSED_STATUSES = (", src)
        start = src.index("CHAT_CLAIM_CLOSED_STATUSES = (")
        block = src[start:start + 220]
        for status in ("assigned", "in_progress", "completed", "cancelled"):
            self.assertIn('"%s"' % status, block)
        self.assertNotIn('"chat_search"', block)
        self.assertNotIn('"searching"', block)


if __name__ == "__main__":
    unittest.main()
