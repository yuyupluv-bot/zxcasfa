"""Opt-in, durable VK notices about a shortage of available drivers."""
from __future__ import annotations

import datetime as dt
import threading
import time

from sqlalchemy import or_
from sqlalchemy.orm import Session

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import Order, OutboxMessage, ROLE_DRIVER, User
from . import outbox_service, queue_service
from .vk_client import vk

log = get_logger("bot.shortage_notices")
POLL_SECONDS = 5.0
NOTICE = "❗ Не хватка водителей на линии, есть свободные заявки."
WAITING_STATUSES = ("created", "queued", "no_drivers", "chat_search", "searching")
_started = False
_lock = threading.Lock()


def waiting_query(session: Session):
    return session.query(Order.id).filter(
        Order.status.in_(WAITING_STATUSES),
        Order.driver_id.is_(None),
        Order.parallel_driver_id.is_(None),
    )


def free_driver_exists(session: Session) -> bool:
    # The queue service accounts for busy trips, offers and temporary absence.
    drivers = session.query(User).filter(User.is_on_line.is_(True)).all()
    return any(status == "online" for status in queue_service.actual_driver_statuses(session, drivers).values())


def reconcile(session: Session) -> None:
    pending = waiting_query(session)
    has_any_waiting = pending.first() is not None
    # An offer currently being considered is not yet taken (and therefore
    # prevents clearing), but is not a free order for a second driver.
    has_old_waiting = pending.filter(
        Order.offered_driver_id.is_(None),
        Order.created_at <= time_utils.now() - dt.timedelta(seconds=60),
    ).first() is not None
    free = free_driver_exists(session)
    should_send = has_old_waiting and not free
    # Clear only when both requested conditions hold simultaneously.
    should_clear = not has_any_waiting and free
    drivers = (session.query(User).filter(or_(
        User.shortage_notice_enabled.is_(True), User.shortage_notice_outbox_id.isnot(None),
    )).order_by(User.id).with_for_update(skip_locked=True).all())
    for driver in drivers:
        current = session.get(OutboxMessage, driver.shortage_notice_outbox_id) if driver.shortage_notice_outbox_id else None
        opted_in = driver.shortage_notice_enabled and not driver.is_blocked and driver.has_role(ROLE_DRIVER)
        if not opted_in or should_clear:
            if driver.shortage_notice_outbox_id:
                outbox_service.cancel_or_delete(session, driver.shortage_notice_outbox_id)
                driver.shortage_notice_outbox_id = None
        # A permanently rejected VK message must not be re-enqueued every
        # poll; retain its row until the shortage episode is cleared.
        elif should_send and current is None:
            if driver.shortage_notice_outbox_id:
                outbox_service.cancel_or_delete(session, driver.shortage_notice_outbox_id)
                driver.shortage_notice_outbox_id = None
            notice_id = vk.send_tracked_message(driver.vk_id, NOTICE)
            if notice_id:
                driver.shortage_notice_outbox_id = notice_id
            else:
                log.error("Could not queue shortage notice for driver=%s", driver.id)


def _worker() -> None:
    while True:
        try:
            with session_scope() as session:
                reconcile(session)
        except Exception:
            log.exception("Shortage notice reconciliation failed")
        time.sleep(POLL_SECONDS)


def start_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_worker, name="driver-shortage-notices", daemon=True).start()
        _started = True
