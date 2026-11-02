"""Atomic booking cancellation initiated from the web admin panel."""
from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from common import time_utils
from common.models import Booking, OutboxMessage, ScheduledJob, User

from . import keyboards as kb
from . import outbox_service
from .admin_order_cancel_service import cancel_from_admin as cancel_order_from_admin
from .states_service import States, reset

ACTIVE_BOOKING_STATUSES = ("pending", "assigned", "driver_en_route")


def _enqueue(session: Session, user: User | None, text: str, keyboard=None) -> None:
    if user is None:
        return
    session.add(OutboxMessage(
        peer_id=user.vk_id,
        text=text[:4000],
        keyboard=keyboard,
        random_id=secrets.randbits(62) or 1,
        status="pending",
        priority=1000,
        attempts=0,
        next_attempt_at=time_utils.now(),
    ))


def cancel_from_admin(session: Session, booking: Booking) -> dict:
    """Cancel one booking, its chat card, timers and linked active order."""
    booking = (
        session.query(Booking)
        .filter(Booking.id == booking.id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if booking is None or booking.status not in ACTIVE_BOOKING_STATUSES:
        return {"changed": False, "previous_status": None}

    previous_status = booking.status
    booking_number = booking.number
    passenger = session.get(User, booking.passenger_id) if booking.passenger_id else None
    driver = session.get(User, booking.driver_id) if booking.driver_id else None

    # Change status before touching side effects so every bot callback sees the
    # booking as closed even if it wakes while this transaction is committing.
    booking.status = "canceled"
    booking.canceled_by = "admin"

    session.query(ScheduledJob).filter(
        ScheduledJob.kind == "booking_chat",
        ScheduledJob.object_id == booking.id,
    ).delete(synchronize_session=False)

    if booking.chat_notice_outbox_id:
        outbox_service.cancel_or_delete(session, booking.chat_notice_outbox_id)
        booking.chat_notice_outbox_id = None

    linked_order_cancelled = False
    if booking.order_id:
        from common.models import Order

        order = session.get(Order, booking.order_id)
        if order is not None and order.status not in ("completed", "cancelled"):
            result = cancel_order_from_admin(session, order, "Отмена брони")
            linked_order_cancelled = bool(result.get("changed"))

    # A linked departed order already queues notifications and restores the
    # driver's active menu through the ordinary admin-order cancellation path.
    if not linked_order_cancelled:
        if passenger:
            reset(session, passenger.vk_id,
                  States.DISP_MENU if passenger.role == "dispatcher" else States.MAIN_MENU)
            passenger_keyboard = (
                kb.dispatcher_menu(False)
                if passenger.role == "dispatcher"
                else kb.passenger_menu(False)
            )
            _enqueue(
                session,
                passenger,
                f"❌ Ваша бронь №{booking_number} отменена администратором.",
                passenger_keyboard,
            )
        if driver:
            _enqueue(
                session,
                driver,
                f"❌ Бронь №{booking_number} отменена администратором.",
                kb.driver_menu(on_line=bool(driver.is_on_line)),
            )

    # Keep the canceled row for the admin history/Excel, but remove it from the
    # driver's active-booking list immediately.
    booking.driver_id = None
    return {
        "changed": True,
        "previous_status": previous_status,
        "linked_order_cancelled": linked_order_cancelled,
    }
