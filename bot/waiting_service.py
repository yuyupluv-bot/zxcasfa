"""Paid waiting during the ride (requirement 2).

Flow:
  * driver marks «arrived / Заезд» and waiting starts automatically;
  * «Пассажир сел» stops the automatic waiting window;
  * during the ride the driver can start another waiting window manually;
  * all waiting windows share one free balance of
    ``free_waiting_minutes`` for the entire order;
  * «Продолжить поездку» stops the meter (can be toggled repeatedly);
  * at finish the total minutes and cost are frozen on the order.

Accumulation is stored directly on the order so it survives across the
long-poll thread and the timer threads:
  * ``waiting_started_at`` – when the current waiting window started (or NULL);
  * ``waiting_seconds``    – accumulated waiting seconds from finished windows;
  * ``waiting_minutes`` / ``waiting_cost`` – finalised totals (for receipts).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common.models import Order
from common.settings_service import get_float, get_int


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def is_running(order: Order) -> bool:
    return order.waiting_started_at is not None


def start_waiting(session: Session, order: Order) -> None:
    if order.waiting_started_at is not None:
        return  # already running
    order.waiting_started_at = _now()


def stop_waiting(session: Session, order: Order) -> int:
    """Stop the current waiting window, returning its length in seconds."""
    if order.waiting_started_at is None:
        return 0
    elapsed = int((_now() - _aware(order.waiting_started_at)).total_seconds())
    elapsed = max(elapsed, 0)
    order.waiting_seconds = int(order.waiting_seconds or 0) + elapsed
    if order.status == "in_progress":
        order.ride_waiting_seconds = int(order.ride_waiting_seconds or 0) + elapsed
    order.waiting_started_at = None
    return elapsed


def current_seconds(order: Order) -> int:
    """Accumulated seconds including the window in progress (read-only)."""
    total = int(order.waiting_seconds or 0)
    if order.waiting_started_at is not None:
        total += max(int((_now() - _aware(order.waiting_started_at)).total_seconds()), 0)
    return total


def current_ride_seconds(order: Order) -> int:
    """Manual waiting consumed since «Пассажир сел», including current run."""
    total = int(order.ride_waiting_seconds or 0)
    if order.status == "in_progress" and order.waiting_started_at is not None:
        total += max(int((_now() - _aware(order.waiting_started_at)).total_seconds()), 0)
    return total


def free_remaining_seconds(session: Session, order: Order) -> int:
    """Remaining free time across arrival and in-ride waiting."""
    free_seconds = max(get_int(session, "free_waiting_minutes", 3), 0) * 60
    return max(free_seconds - current_seconds(order), 0)


def _cost_for(session: Session, total_seconds: int, ride_seconds: int = 0) -> tuple[int, float]:
    free = get_int(session, "free_waiting_minutes", 3)
    rate = get_float(session, "price_per_waiting_minute", 10)
    # Keep the existing rounding convention, applied once to the total.
    minutes = int(round(max(int(total_seconds), 0) / 60))
    billable = max(minutes - max(free, 0), 0)
    return minutes, round(billable * rate, 2)


def snapshot(session: Session, order: Order) -> tuple[int, float]:
    """Return (minutes, cost) so far without mutating the order."""
    return _cost_for(session, current_seconds(order), current_ride_seconds(order))


def finalize(session: Session, order: Order) -> tuple[int, float]:
    """Freeze waiting totals on the order and return (minutes, cost)."""
    stop_waiting(session, order)
    minutes, cost = _cost_for(
        session,
        int(order.waiting_seconds or 0),
        int(order.ride_waiting_seconds or 0),
    )
    order.waiting_minutes = minutes
    order.waiting_cost = cost
    return minutes, cost
