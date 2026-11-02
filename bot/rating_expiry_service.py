"""Durable 12-hour expiry for passenger and driver rating cards.

The original tracked VK message is finalized in place: stars and «Пропустить»
disappear and the expiry note is appended.  No separate expiry message is sent.
"""
from __future__ import annotations

import datetime as dt

from common import time_utils
from common.database import session_scope
from common.models import Order, OutboxMessage, ScheduledJob, User

from . import outbox_service, timers

RATING_EXPIRY_SECONDS = 12 * 60 * 60
EXPIRY_TEXT = "Время для оценки истекло."

_PASSENGER_KIND = "passenger_rating_expire"
_DRIVER_KIND = "driver_rating_expire"
_PASSENGER_FIELD = "passenger_rating_prompt_outbox_id"
_DRIVER_FIELD = "driver_rating_prompt_outbox_id"


def _kind(driver_side: bool) -> str:
    return _DRIVER_KIND if driver_side else _PASSENGER_KIND


def _field(driver_side: bool) -> str:
    return _DRIVER_FIELD if driver_side else _PASSENGER_FIELD


def schedule_expiry(
    order_id: int,
    *,
    driver_side: bool,
    delay_seconds: float | None = None,
) -> None:
    """Start or replace the durable timer for one side of one completed trip."""
    order_id = int(order_id)
    timers.schedule(
        _kind(driver_side),
        order_id,
        RATING_EXPIRY_SECONDS if delay_seconds is None else max(0.0, delay_seconds),
        lambda: expire(order_id, driver_side=driver_side),
    )


def cancel_expiry(order_id: int, *, driver_side: bool) -> None:
    """Cancel the matching timer after rating or explicit skip."""
    timers.cancel(_kind(driver_side), int(order_id))


def schedule_missing_for_open_prompts() -> int:
    """Cover rating cards created before this release or a partial restart."""
    scheduled = 0
    with session_scope() as session:
        existing = {
            (str(kind), int(object_id))
            for kind, object_id in session.query(
                ScheduledJob.kind,
                ScheduledJob.object_id,
            ).filter(
                ScheduledJob.kind.in_((_PASSENGER_KIND, _DRIVER_KIND)),
                ScheduledJob.status == "pending",
            ).all()
        }
        orders = session.query(Order).filter(
            (Order.passenger_rating_prompt_outbox_id.isnot(None))
            | (Order.driver_rating_prompt_outbox_id.isnot(None))
        ).all()
        now = time_utils.now()
        for order in orders:
            for driver_side, field in (
                (False, _PASSENGER_FIELD),
                (True, _DRIVER_FIELD),
            ):
                outbox_id = getattr(order, field, None)
                kind = _kind(driver_side)
                if not outbox_id or (kind, int(order.id)) in existing:
                    continue
                outbox = session.get(OutboxMessage, int(outbox_id))
                started_at = (
                    (outbox.created_at if outbox else None)
                    or order.completed_at
                    or now
                )
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=dt.timezone.utc)
                elapsed = max(0.0, (now - started_at).total_seconds())
                schedule_expiry(
                    order.id,
                    driver_side=driver_side,
                    # Give the surrounding startup transaction enough time to
                    # commit the persistent job before an overdue callback runs.
                    delay_seconds=max(1.0, RATING_EXPIRY_SECONDS - elapsed),
                )
                scheduled += 1
    return scheduled


def expire(order_id: int, *, driver_side: bool) -> None:
    """Finalize the still-open rating card in its original VK message.

    Locking the order serializes this callback with rating/skip handlers.  The
    prompt outbox id is the durable authority: once another action clears it,
    an overdue or replayed timer becomes a harmless no-op.
    """
    field = _field(driver_side)
    with session_scope() as session:
        order = (
            session.query(Order)
            .filter(Order.id == int(order_id))
            .with_for_update()
            .one_or_none()
        )
        if order is None:
            return
        outbox_id = getattr(order, field, None)
        if not outbox_id:
            return

        row = session.get(OutboxMessage, int(outbox_id))
        if row is None:
            setattr(order, field, None)
            return

        compact_keyboard = (row.keyboard or "").replace(" ", "").lower()
        already_finalized = (
            EXPIRY_TEXT in (row.text or "")
            and '"buttons":[]' in compact_keyboard
        )
        if already_finalized:
            setattr(order, field, None)
            return
        if row.status in ("cancelled", "delete_requested", "deleting"):
            setattr(order, field, None)
            return

        if not outbox_service.finalize_tracked_message(
            session,
            int(outbox_id),
            EXPIRY_TEXT,
            defer_edit=True,
        ):
            # The persistent scheduler retries failed callbacks. This covers a
            # transient VK edit failure without creating a second message.
            raise RuntimeError(
                f"could not expire rating prompt order={order.id} driver_side={driver_side}"
            )
        setattr(order, field, None)
        if not driver_side and order.passenger_id:
            # Delivery completion historically left the passenger in P_RATE.
            # Clear only that exact order's stale state; never overwrite a
            # newer flow the passenger has already started during the 12h.
            passenger = session.get(User, order.passenger_id)
            if passenger:
                from .states_service import States, get_data, get_state, reset

                state = get_state(session, passenger.vk_id)
                data = get_data(session, passenger.vk_id)
                try:
                    state_order_id = int(data.get("order_id") or 0)
                except (TypeError, ValueError):
                    state_order_id = 0
                if state.state == States.P_RATE and state_order_id == order.id:
                    reset(session, passenger.vk_id, States.MAIN_MENU)
