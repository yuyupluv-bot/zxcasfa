"""Transactional, retrying delivery of VK messages."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import Booking, DriverQueue, Order, OutboxMessage

log = get_logger("bot.outbox")
_started = False
_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _is_permanent_vk_error(error: str) -> bool:
    """Errors that retries cannot fix without user action or code changes."""
    return bool(re.search(r"\[(?:901|911)\]", error or ""))


_ORDER_ACTION_COMMANDS = {
    "accept", "decline", "decline_reason", "pending_take",
    "chat_take", "chat_no_driver", "parallel_take", "route_parallel_take",
    "parallel_route_decline", "queue_yes", "queue_no",
    "chat_order_actual_yes", "chat_order_actual_no",
    "order_status", "departure_wait", "departure_cancel",
}


def _order_action_ids(row: OutboxMessage) -> set[int]:
    """Extract order ids from driver/passenger action cards in the outbox."""
    result: set[int] = set()
    if row.keyboard:
        try:
            keyboard = json.loads(row.keyboard)
        except (TypeError, ValueError, json.JSONDecodeError):
            keyboard = {}
        for buttons in keyboard.get("buttons") or []:
            for button in buttons or []:
                action = button.get("action") if isinstance(button, dict) else None
                payload = action.get("payload") if isinstance(action, dict) else None
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                if not isinstance(payload, dict) or payload.get("cmd") not in _ORDER_ACTION_COMMANDS:
                    continue
                try:
                    result.add(int(payload.get("order_id")))
                except (TypeError, ValueError):
                    pass
    # Modern cards carry the permanent internal ID in their payload. The
    # printed number is resettable and must never be treated as a primary key.
    if result:
        return result
    # Compatibility only for old cards that had no usable action payload.
    match = re.match(r"^🔔 Новая заявка #(\d+)(?!\d)", row.text or "")
    if match:
        result.add(int(match.group(1)))
    return result


def _cancel_if_order_is_stale(session, row: OutboxMessage) -> bool:
    """Never deliver action cards for an order already cancelled in admin."""
    # Drop only the retired factory refusal summary still pending after an
    # upgrade. Do not remove a passenger's forwarded quotation of that text.
    if (row.text or "").lstrip().startswith("Все водители отказались от вашей заявки."):
        row.status = "cancelled"
        row.claimed_at = None
        row.last_error = "suppressed retired refusal summary v176"
        return True
    order_ids = _order_action_ids(row)
    if not order_ids:
        return False
    stale_ids = []
    compact_keyboard = (row.keyboard or "").replace(" ", "").lower()
    waiting_menu = '"inline":true' not in compact_keyboard and "order_status" in compact_keyboard
    for order_id in order_ids:
        order = session.get(Order, order_id)
        if (order is None or order.status in ("cancelled", "completed")
                or (waiting_menu and order.status in ("assigned", "parallel_assigned", "arrived", "in_progress"))):
            # A delayed old waiting menu must not overwrite the passenger's
            # newly installed main ride/arrival keyboard after ETA is sent.
            stale_ids.append(order_id)
    if not stale_ids:
        return False
    row.status = "cancelled"
    row.claimed_at = None
    row.last_error = "suppressed stale order card: " + ",".join(map(str, sorted(stale_ids)))
    log.info("Suppressed stale outbox card=%s orders=%s", row.id, stale_ids)
    return True


def _cancel_if_queue_front_is_stale(session, row: OutboxMessage) -> bool:
    """Never deliver an old first-place promise to today's second driver."""
    legacy_front = (row.priority == 900 and (row.text or "").lstrip().startswith("🥇 Вы первый в очереди!"))
    if row.priority != 901 and not legacy_front:
        return False
    from . import queue_service
    entry = session.query(DriverQueue).filter(
        DriverQueue.front_notice_outbox_id == row.id,
    ).one_or_none()
    if entry and queue_service.is_current_queue_front(session, entry):
        return False
    if entry:
        entry.front_notified = False
        entry.front_notice_outbox_id = None
    if row.status == "sent":
        # The position can change while messages.send is in flight. Keep VK
        # ids so the delete worker can retire a just-delivered stale notice.
        row.status = "delete_requested"
        row.next_attempt_at = time_utils.now()
        row.last_error = (row.last_error or "") + ";suppressed stale queue-front notice"
    else:
        row.status = "cancelled"
        row.last_error = "suppressed stale queue-front notice"
    row.claimed_at = None
    log.info("Suppressed stale queue-front notice outbox=%s peer=%s", row.id, row.peer_id)
    return True


def _stored_vk_marker_id(row: OutboxMessage, key: str) -> int | None:
    marker = row.last_error or ""
    match = re.search(rf"(?:^|;){re.escape(key)}:(\d+)(?:;|$)", marker)
    return int(match.group(1)) if match else None


def _stored_vk_message_id(row: OutboxMessage) -> int | None:
    return _stored_vk_marker_id(row, "vk_message_id")


def _stored_vk_cmid(row: OutboxMessage) -> int | None:
    return _stored_vk_marker_id(row, "vk_cmid")


def has_usable_vk_id(row: OutboxMessage) -> bool:
    global_id = _stored_vk_message_id(row)
    cmid = _stored_vk_cmid(row)
    return bool((global_id is not None and global_id > 0) or cmid)


def record_outgoing_message_event(session, message: dict) -> bool:
    """Persist cmid from VK's MESSAGE_REPLY or outgoing MESSAGE_NEW event."""
    if not isinstance(message, dict):
        return False
    peer_id = int(message.get("peer_id") or 0)
    cmid = int(message.get("conversation_message_id") or 0)
    random_id = int(message.get("random_id") or 0)
    if peer_id <= 0 or cmid <= 0:
        return False
    query = session.query(OutboxMessage).filter(OutboxMessage.peer_id == peer_id)
    row = None
    if random_id:
        row = (
            query.filter(OutboxMessage.random_id == random_id)
            .order_by(OutboxMessage.id.desc())
            .first()
        )
    if row is None:
        # Some VK Long Poll payloads omit random_id. Match a recent unresolved
        # outgoing row by its exact text; oldest first preserves event order for
        # identical aggregate notices.
        text = str(message.get("text") or "")
        cutoff = time_utils.now() - dt.timedelta(minutes=5)
        rows = (
            query.filter(
                OutboxMessage.text == text,
                OutboxMessage.created_at >= cutoff,
            )
            .order_by(OutboxMessage.id.asc())
            .limit(100)
            .all()
        )
        row = next((item for item in rows if not _stored_vk_cmid(item)), None)
    if row is None:
        # A driver may claim the request before VK's outgoing event arrives.
        # Such a row already contains the final text, while the event contains
        # the original card. Match by the preserved original-text prefix.
        text = str(message.get("text") or "")
        cutoff = time_utils.now() - dt.timedelta(minutes=5)
        rows = (
            query.filter(
                OutboxMessage.status.in_(("finalize_requested", "finalizing")),
                OutboxMessage.created_at >= cutoff,
            )
            .order_by(OutboxMessage.id.asc())
            .limit(100)
            .all()
        )
        row = next(
            (item for item in rows if (item.text or "").startswith(text + "\n\n")),
            None,
        )
    if row is None:
        log.warning("VK outgoing event cmid=%s peer=%s had no outbox match", cmid, peer_id)
        return False
    message_id = int(message.get("id") or 0)
    previous_error = row.last_error or ""
    row.last_error = f"vk_message_id:{message_id};vk_cmid:{cmid}"
    if row.status == "cancelled" and "undeletable_missing_vk_id" in previous_error:
        row.status = "delete_requested"
        row.next_attempt_at = time_utils.now()
    log.info(
        "Captured VK outgoing event outbox=%s peer=%s message_id=%s cmid=%s",
        row.id, peer_id, message_id, cmid,
    )
    return True


def _recover_undeliverable_driver_offer(session, row: OutboxMessage, error: str) -> bool:
    """Release an offer immediately when VK permanently rejects the driver.

    Error 901 means the community cannot message this user. Keeping that user
    in `offered` made all following orders wait behind an unreachable driver.
    Only messages with the exact ordinary-offer prefix are handled here.
    """
    if "901" not in (error or ""):
        return False
    match = re.search(r"Новая заявка #(\d+)", row.text or "")
    if not match:
        return False
    from common.models import Order, User
    from . import order_service, queue_service

    order = session.get(Order, int(match.group(1)))
    driver = session.query(User).filter(User.vk_id == row.peer_id).one_or_none()
    if not order or not driver:
        return False
    if order.status != "searching" or order.offered_driver_id != driver.id:
        return False

    log.error(
        "VK 901 for offered driver: order=%s driver=%s; removing unreachable driver and redispatching",
        order.id,
        driver.id,
    )
    order_service._set_current_offer(session, order, None)
    queue_service.leave_queue(session, driver)
    driver.is_on_line = False
    order.status = "searching"
    order_service.offer_to_next_driver(session, order)
    return True


def _recover_undeliverable_payment_details(session, row: OutboxMessage, error: str) -> bool:
    """Make the payment button available again after a permanent VK failure."""
    text = row.text or ""
    if "Реквизиты для оплаты" not in text:
        return False
    match = re.search(r"(?:заявке|поездке) #(\d+)", text)
    if not match:
        return False
    from common.models import Order, User
    from .vk_client import vk

    order = session.get(Order, int(match.group(1)))
    if not order:
        return False
    order.payment_details_sent = False
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if driver:
        vk.send_message(
            driver.vk_id,
            f"⚠️ Реквизиты по заявке #{order.number} не дошли до пассажира. "
            "Проверьте, что пассажир может получать сообщения сообщества, и отправьте ещё раз.",
        )
    log.error(
        "Payment details delivery failed permanently: order=%s peer=%s error=%s",
        order.id, row.peer_id, error,
    )
    return True


def _claim_batch(limit: int = 50) -> list[int]:
    now = time_utils.now()
    # Only startup recovers interrupted sends. Reclaiming a live slow
    # request by age could duplicate it; runtime_guard restarts stuck owners.
    with session_scope() as session:
        rows = (
            session.query(OutboxMessage)
            .filter(
                OutboxMessage.status.in_(("pending", "failed")),
                OutboxMessage.next_attempt_at <= now,
            )
            .order_by(OutboxMessage.priority.desc(), OutboxMessage.id.asc())
            .with_for_update(skip_locked=True)
            .limit(max(1, limit))
            .all()
        )
        ids = []
        for row in rows:
            row.status = "sending"
            row.claimed_at = now
            ids.append(row.id)
        return ids


def _deliver_one(message_id: int) -> None:
    try:
        with session_scope() as session:
            row = session.get(OutboxMessage, message_id)
            if not row or row.status != "sending":
                return
            if _cancel_if_order_is_stale(session, row):
                return
            if _cancel_if_queue_front_is_stale(session, row):
                return
            payload = (
                row.peer_id,
                row.text or "",
                row.keyboard,
                row.attachment,
                row.random_id,
                bool(row.dont_parse_links),
            )
        from .vk_client import vk
        vk_message_id = vk._send_now_result(*payload)
        vk_cmid = vk.last_conversation_message_id()
        with session_scope() as session:
            row = session.get(OutboxMessage, message_id)
            if not row:
                return
            # MESSAGE_REPLY can arrive before messages.send returns. Preserve
            # the authoritative cmid already captured by the event worker.
            if not vk_cmid:
                vk_cmid = _stored_vk_cmid(row)
            row.attempts = (row.attempts or 0) + 1
            row.claimed_at = None
            if row.status == "cancel_requested":
                row.sent_at = time_utils.now()
                marker = f"vk_message_id:{vk_message_id}" if vk_message_id is not None else ""
                if vk_cmid:
                    marker += f";vk_cmid:{vk_cmid}"
                deleted = bool(
                    (vk_message_id is not None or vk_cmid)
                    and vk.delete_message(row.peer_id, vk_message_id, vk_cmid)
                )
                if deleted:
                    row.status = "cancelled"
                    row.last_error = "vk_message_deleted_for_all"
                    session.query(Order).filter(
                        Order.chat_notice_outbox_id == row.id
                    ).update({Order.chat_notice_outbox_id: None}, synchronize_session=False)
                    session.query(Booking).filter(
                        Booking.chat_notice_outbox_id == row.id
                    ).update({Booking.chat_notice_outbox_id: None}, synchronize_session=False)
                else:
                    row.status = "delete_requested"
                    row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=2)
                    row.last_error = marker
                    log.warning("VK deletion queued for retry outbox=%s peer=%s", row.id, row.peer_id)
            elif vk_message_id is not None:
                finalize_after_send = row.status == "finalize_requested"
                row.status = "finalize_requested" if finalize_after_send else "sent"
                row.sent_at = time_utils.now()
                # Reuse an existing nullable TEXT column; no production migration
                # is required merely to remember an id for later deletion.
                row.last_error = f"vk_message_id:{vk_message_id}"
                if vk_cmid:
                    row.last_error += f";vk_cmid:{vk_cmid}"
                if finalize_after_send:
                    row.next_attempt_at = time_utils.now()
                else:
                    _cancel_if_queue_front_is_stale(session, row)
            else:
                send_error = vk.last_send_error() or "VK API send failed"
                if _recover_undeliverable_driver_offer(session, row, send_error):
                    row.status = "cancelled"
                    row.last_error = f"permanent delivery failure recovered: {send_error[:500]}"
                elif _is_permanent_vk_error(send_error):
                    _recover_undeliverable_payment_details(session, row, send_error)
                    row.status = "cancelled"
                    row.last_error = f"permanent delivery failure: {send_error[:500]}"
                else:
                    row.status = "failed"
                    delay = min(300, 2 ** min(row.attempts, 8))
                    row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=delay)
                    row.last_error = f"{send_error[:500]}; scheduled retry"
    except Exception as exc:  # noqa: BLE001
        # Never leave a driver offer permanently in `sending` when one worker
        # crashes. Return it to retry immediately and keep the exception in logs.
        log.exception("Outbox delivery crashed message_id=%s: %s", message_id, exc)
        try:
            with session_scope() as session:
                row = session.get(OutboxMessage, message_id)
                if row and row.status == "sending":
                    row.status = "failed"
                    row.claimed_at = None
                    row.next_attempt_at = time_utils.now()
                    row.last_error = f"worker crash: {str(exc)[:500]}"
        except Exception:  # noqa: BLE001
            log.exception("Could not release crashed outbox message_id=%s", message_id)



def _claim_delete_batch(limit: int = 50, exclude_ids: tuple[int, ...] = ()) -> list[int]:
    now = time_utils.now()
    with session_scope() as session:
        rows = (
            session.query(OutboxMessage)
            .filter(
                OutboxMessage.status == "delete_requested",
                OutboxMessage.id.notin_(exclude_ids),
                OutboxMessage.next_attempt_at <= now,
            )
            .order_by(OutboxMessage.id.asc())
            .with_for_update(skip_locked=True)
            .limit(max(1, limit))
            .all()
        )
        ids: list[int] = []
        for row in rows:
            row.status = "deleting"
            row.claimed_at = now
            ids.append(row.id)
        return ids


def _delete_one(message_id: int) -> None:
    with session_scope() as session:
        row = session.get(OutboxMessage, message_id)
        if not row or row.status != "deleting":
            return
        peer_id = row.peer_id
        global_id = _stored_vk_message_id(row)
        cmid = _stored_vk_cmid(row)
        has_id = bool((global_id is not None and global_id > 0) or cmid)
    if not has_id:
        # A fresh chat send may receive its authoritative cmid asynchronously
        # through MESSAGE_REPLY. Wait briefly; old legacy rows still terminate.
        age = None
        if row.sent_at:
            sent_at = row.sent_at
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
            age = (time_utils.now() - sent_at).total_seconds()
        if age is not None and age < 30:
            with session_scope() as session:
                row = session.get(OutboxMessage, message_id)
                if row:
                    row.status = "delete_requested"
                    row.claimed_at = None
                    row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=2)
            return
        with session_scope() as session:
            row = session.get(OutboxMessage, message_id)
            if row:
                row.status = "cancelled"
                row.claimed_at = None
                row.last_error = "undeletable_missing_vk_id"
        # Legacy rows can be numerous. Their retry is terminated once, but do
        # not flood production logs with one warning per historical row.
        log.debug("Stopped VK delete retries without ids outbox=%s peer=%s", message_id, peer_id)
        return
    from .vk_client import vk
    deleted = vk.delete_message(peer_id, global_id, cmid)
    with session_scope() as session:
        row = session.get(OutboxMessage, message_id)
        if not row:
            return
        row.claimed_at = None
        row.attempts = (row.attempts or 0) + 1
        if deleted:
            row.status = "cancelled"
            row.last_error = "vk_message_deleted_for_all"
            session.query(Order).filter(
                Order.chat_notice_outbox_id == row.id
            ).update({Order.chat_notice_outbox_id: None}, synchronize_session=False)
            session.query(Booking).filter(
                Booking.chat_notice_outbox_id == row.id
            ).update({Booking.chat_notice_outbox_id: None}, synchronize_session=False)
            log.info("Retried VK deletion succeeded outbox=%s peer=%s", row.id, row.peer_id)
        else:
            row.status = "delete_requested"
            delay = min(300, 2 ** min(row.attempts, 8))
            row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=delay)
            log.warning("VK deletion retry scheduled outbox=%s in %ss", row.id, delay)


def update_tracked_message(session, message_id: int, text: str) -> bool:
    """Update one queued/sent notice without creating another chat message."""
    row = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.id == int(message_id))
        .with_for_update()
        .one_or_none()
    )
    if not row:
        return False
    if row.status in ("pending", "failed"):
        row.text = text
        row.status = "pending"
        row.claimed_at = None
        row.next_attempt_at = time_utils.now()
        return True
    if row.status != "sent":
        return False
    vk_message_id = _stored_vk_message_id(row)
    vk_cmid = _stored_vk_cmid(row)
    if vk_message_id is None and not vk_cmid:
        return False
    from .vk_client import vk
    if not vk.edit_message(
        row.peer_id,
        vk_message_id,
        text,
        row.keyboard,
        vk_cmid,
        row.attachment,
    ):
        return False
    row.text = text
    return True


def finalize_tracked_message(
    session,
    message_id: int,
    text: str,
    attachment: str | None = None,
    replace_text: bool = False,
    replacement_keyboard: str | None = None,
    defer_edit: bool = False,
) -> bool:
    """Finalize a card; optionally replace choices with follow-up controls."""
    row = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.id == int(message_id))
        .with_for_update()
        .one_or_none()
    )
    if not row:
        return False
    # Callers that know the business attachment may restore it on legacy rows
    # where an earlier queued card failed to persist the voice reference.
    if attachment:
        row.attachment = attachment
    # Important: an inline card must be cleared with an *inline* empty
    # keyboard. Sending a regular empty keyboard while editing an inline card
    # also removes the user's persistent bottom VK menu (passenger/driver).
    empty_keyboard = '{"buttons":[],"one_time":true}'
    compact_keyboard = (row.keyboard or "").replace(" ", "").lower()
    if '"inline":true' in compact_keyboard:
        empty_keyboard = '{"buttons":[],"inline":true}'
    if replacement_keyboard is not None:
        empty_keyboard = replacement_keyboard
    final_text = text if replace_text else (row.text or "")
    if not replace_text and text not in final_text:
        final_text = final_text.rstrip() + "\n\n" + text
    if defer_edit and "rating_expiry_retry:" not in (row.last_error or ""):
        row.last_error = (row.last_error or "") + ";rating_expiry_retry:0"
    if row.status in ("pending", "failed"):
        row.text = final_text
        row.keyboard = empty_keyboard
        row.status = "pending"
        row.claimed_at = None
        row.next_attempt_at = time_utils.now()
        return True
    if row.status in ("sending", "sent") and not has_usable_vk_id(row):
        row.text = final_text
        row.keyboard = empty_keyboard
        row.status = "finalize_requested"
        row.claimed_at = None
        row.next_attempt_at = time_utils.now()
        return True
    if row.status != "sent":
        return False
    if defer_edit:
        row.text = final_text
        row.keyboard = empty_keyboard
        row.status = "finalize_requested"
        row.claimed_at = None
        row.next_attempt_at = time_utils.now()
        return True
    from .vk_client import vk
    if not vk.edit_message(
        row.peer_id,
        _stored_vk_message_id(row),
        final_text,
        empty_keyboard,
        _stored_vk_cmid(row),
        row.attachment,
    ):
        return False
    row.text = final_text
    row.keyboard = empty_keyboard
    row.last_error = (row.last_error or "") + ";finalized_without_keyboard"
    return True


def _claim_finalize_batch(limit: int = 50, exclude_ids: tuple[int, ...] = ()) -> list[int]:
    now = time_utils.now()
    with session_scope() as session:
        rows = (
            session.query(OutboxMessage)
            .filter(
                OutboxMessage.status == "finalize_requested",
                OutboxMessage.id.notin_(exclude_ids),
                OutboxMessage.next_attempt_at <= now,
            )
            .order_by(OutboxMessage.id.asc())
            .with_for_update(skip_locked=True)
            .limit(max(1, limit))
            .all()
        )
        ids = []
        for row in rows:
            row.status = "finalizing"
            row.claimed_at = now
            ids.append(row.id)
        return ids


def _original_tracked_text(text: str) -> str:
    value = text or ""
    markers = ("\n\n✅ Заявку #", "\n\n💬 Заявку #")
    positions = [value.find(marker) for marker in markers if value.find(marker) >= 0]
    return value[:min(positions)] if positions else value


def _finalize_one(message_id: int) -> None:
    # Snapshot in a short transaction. NO network waits while holding DB locks.
    with session_scope() as session:
        row = session.get(OutboxMessage, message_id)
        if not row or row.status != "finalizing":
            return
        global_id = _stored_vk_message_id(row)
        cmid = _stored_vk_cmid(row)
        peer_id, text, keyboard, attachment = row.peer_id, row.text or "", row.keyboard, row.attachment
    from .vk_client import vk
    if not ((global_id is not None and global_id > 0) or cmid):
        cmid = vk.find_existing_chat_cmid(
            peer_id, _original_tracked_text(text), vk.last_observed_chat_cmid(peer_id),
        )
    ok = bool(((global_id is not None and global_id > 0) or cmid) and
              vk.edit_message(peer_id, global_id, text, keyboard, cmid, attachment))
    attempted = bool((global_id is not None and global_id > 0) or cmid)
    error_code = vk.last_edit_error_code() if attempted and not ok else None
    with session_scope() as session:
        row = session.query(OutboxMessage).filter(OutboxMessage.id == message_id).with_for_update().one_or_none()
        if not row or row.status != "finalizing":
            return
        row.claimed_at = None
        if (row.text or "", row.keyboard, row.attachment) != (text, keyboard, attachment):
            row.status = "finalize_requested"
            row.next_attempt_at = time_utils.now()
            return
        if cmid and not _stored_vk_cmid(row):
            row.last_error = (row.last_error or "") + f";vk_cmid:{cmid}"
        if ok:
            row.status = "sent"
            row.last_error = (row.last_error or "") + ";finalized_without_keyboard"
            return
        marker = re.search(r"rating_expiry_retry:(\d+)", row.last_error or "")
        rating_attempts = int(marker.group(1)) + 1 if marker else 0
        if marker:
            row.last_error = re.sub(r"rating_expiry_retry:\d+", f"rating_expiry_retry:{rating_attempts}", row.last_error)
        # Expiry is authoritative in DB even if VK never lets us edit the UI.
        if error_code in (901, 911) or (marker and rating_attempts >= 8):
            row.status = "sent"  # Original was sent; its cosmetic edit failed.
            row.last_error = (row.last_error or "") + f";edit_abandoned_code:{error_code}"
            log.warning("Stopped VK cosmetic edit outbox=%s code=%s rating_attempts=%s", row.id, error_code, rating_attempts)
            return
        row.status = "finalize_requested"
        row.attempts = (row.attempts or 0) + 1
        delay = min(300, 2 ** min(row.attempts, 8))
        if marker or error_code in (9, 29):
            delay = max(60, min(900, 60 * 2 ** min(rating_attempts, 4)))
        row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=delay)


def cancel_or_delete(session, message_id: int) -> bool:
    """Persistently cancel/delete a message without blocking the caller."""
    row = (
        session.query(OutboxMessage)
        .filter(OutboxMessage.id == int(message_id))
        .with_for_update()
        .one_or_none()
    )
    if not row:
        log.error("Cannot delete VK message: outbox row %s is missing", message_id)
        return False
    if row.status in ("pending", "failed"):
        row.status = "cancelled"
        row.claimed_at = None
        return True
    if row.status in ("sending", "cancel_requested", "deleting", "delete_requested"):
        if row.status in ("sending", "cancel_requested"):
            row.status = "cancel_requested"
        return True
    global_id = _stored_vk_message_id(row)
    cmid = _stored_vk_cmid(row)
    if row.status in ("sent", "cancelled") and (
        (global_id is not None and global_id > 0) or cmid
    ):
        # Never perform VK network I/O while an order transaction is open.
        # The dedicated delete worker picks this up within 250 ms and retries.
        row.status = "delete_requested"
        row.claimed_at = None
        row.next_attempt_at = time_utils.now()
        return True
    if row.status in ("sent", "cancelled"):
        row.status = "cancelled"
        row.claimed_at = None
        row.last_error = "undeletable_missing_vk_id"
        return True
    return row.status == "cancelled"


def _run_outbox_task(callback, message_id):
    from . import runtime_guard
    try:
        return runtime_guard.call("outbox-task", str(message_id), callback, message_id)
    except Exception:
        # Sending has its own recovery. Cosmetic cleanup can be retried after
        # an exception; never silently strand it in a claimed status.
        log.exception("Outbox task failed callback=%s id=%s", callback.__name__, message_id)
        if callback in (_finalize_one, _delete_one):
            with session_scope() as session:
                row = session.get(OutboxMessage, message_id)
                expected, retry = (("finalizing", "finalize_requested") if callback is _finalize_one
                                   else ("deleting", "delete_requested"))
                if row and row.status == expected:
                    row.status, row.claimed_at = retry, None
                    row.next_attempt_at = time_utils.now() + dt.timedelta(seconds=60)
        raise


def _collect_done(active):
    for future in list(active):
        if future.done():
            message_id = active.pop(future)
            try:
                future.result()
            except Exception:
                log.exception("Outbox future failed message_id=%s", message_id)


def _worker() -> None:
    from common.config import config
    from . import runtime_guard
    # Cosmetic edits/deletes cannot occupy all ordinary-message workers.
    cleanup = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vk-outbox-cleanup")
    sending, cleaning = {}, {}
    cleanup_turn = False
    while True:
        runtime_guard.pulse("outbox-loop")
        try:
            _collect_done(sending)
            _collect_done(cleaning)
            slots = config.OUTBOX_WORKERS - len(sending)
            if slots > 0:
                for message_id in _claim_batch(slots):
                    sending[_executor.submit(_run_outbox_task, _deliver_one, message_id)] = message_id
            # Alternate to prevent permanent starvation of deletes or edits.
            slots = 2 - len(cleaning)
            if slots > 0:
                choices = ((_claim_finalize_batch, _finalize_one), (_claim_delete_batch, _delete_one))
                if cleanup_turn:
                    choices = choices[::-1]
                cleanup_turn = not cleanup_turn
                for claim, callback in choices:
                    if slots <= 0:
                        break
                    # Do not edit/delete a row while its original send is
                    # still running, even if a handler changed its DB status.
                    ids = claim(slots, exclude_ids=tuple(sending.values()))
                    for message_id in ids:
                        cleaning[cleanup.submit(_run_outbox_task, callback, message_id)] = message_id
                    slots -= len(ids)
            time.sleep(0.1 if sending or cleaning else 0.25)
        except Exception as exc:  # noqa: BLE001
            log.exception("Outbox worker failed: %s", exc)
            time.sleep(1)


def start_worker() -> None:
    global _started, _executor
    with _lock:
        if _started:
            return
        from common.config import config
        # On a clean restart no previous process can still own these rows.
        # Recover all interrupted sends immediately instead of waiting for TTL.
        with session_scope() as session:
            recovered = session.query(OutboxMessage).filter(
                OutboxMessage.status == "sending"
            ).update(
                {
                    "status": "pending",
                    "claimed_at": None,
                    "next_attempt_at": time_utils.now(),
                },
                synchronize_session=False,
            )
            interrupted_cancels = session.query(OutboxMessage).filter(
                OutboxMessage.status == "cancel_requested"
            ).all()
            for row in interrupted_cancels:
                if _stored_vk_message_id(row) is not None or _stored_vk_cmid(row):
                    row.status = "delete_requested"
                    row.next_attempt_at = time_utils.now()
                else:
                    row.status = "cancelled"
                row.claimed_at = None
            session.query(OutboxMessage).filter(
                OutboxMessage.status == "finalizing"
            ).update(
                {
                    "status": "finalize_requested",
                    "claimed_at": None,
                    "next_attempt_at": time_utils.now(),
                },
                synchronize_session=False,
            )
            session.query(OutboxMessage).filter(
                OutboxMessage.status == "deleting"
            ).update(
                {"status": "delete_requested", "claimed_at": None,
                 "next_attempt_at": time_utils.now()}, synchronize_session=False,
            )
        if recovered:
            log.warning("Recovered %s interrupted outbox messages on startup", recovered)
        _executor = ThreadPoolExecutor(
            max_workers=config.OUTBOX_WORKERS,
            thread_name_prefix="vk-outbox-send",
        )
        threading.Thread(target=_worker, name="vk-outbox", daemon=True).start()
        _started = True
        log.info("VK outbox worker started")
