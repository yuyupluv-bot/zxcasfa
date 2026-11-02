"""Driver queue management (drivers_queue table).

Queue model (requirement 14 — standard FIFO of free drivers):
  * A driver going ONLINE (from offline) is appended to the TAIL.
  * A driver RETURNING from 'away' is appended to the TAIL.
  * A driver FINISHING a ride is appended to the TAIL (loses old position).
  * New offers are always sent to the HEAD of the waiting list.

Statuses stored on the queue row: 'waiting' (free), 'offered' (one pending
offer), 'assigned' (busy), 'away'.
Drivers not in the queue at all are considered offline / not on line.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from common.models import ROLE_DRIVER, City, DriverQueue, DriverSelection, Order, User
from common.logger import get_logger

from .roles import STATUS_ORDER

log = get_logger("bot.queue")

# Sort weight for queue rows (free first, then away, then busy).
_QUEUE_STATUS_ORDER = {"waiting": 0, "offered": 1, "away": 2, "assigned": 3}


def _sync_away_notice(session: Session, driver: User) -> None:
    """Immediately create/remove the driver's aggregate away notification."""
    from . import away_order_notice_service
    # Any transition out of ``away`` invalidates the persistent 30-minute
    # timeout. ``set_away`` schedules/replaces it explicitly below.
    if driver.driver_status != "away":
        from . import driver_away_timeout_service
        driver_away_timeout_service.cancel(driver.id)
    away_order_notice_service.sync_driver(session, driver)


def _max_position(session: Session) -> int:
    return session.query(func.coalesce(func.max(DriverQueue.position), 0)).scalar() or 0


def _min_position(session: Session) -> int:
    return session.query(func.coalesce(func.min(DriverQueue.position), 0)).scalar() or 0


def _move_to_tail(session: Session, entry: DriverQueue) -> None:
    entry.position = _max_position(session) + 1


def join_queue(session: Session, driver: User, city_id: int | None) -> DriverQueue:
    """Put a driver on line at the TAIL of the queue as free ('waiting')."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        # Repeated VK button events must be idempotent.  Moving an already-free
        # driver to the tail on every duplicate click made the queue jump.
        already_waiting_here = bool(
            entry.status == "waiting"
            and entry.city_id == city_id
            and driver.is_on_line
            and driver.driver_status == "online"
        )
        entry.status = "waiting"
        entry.city_id = city_id
        if not already_waiting_here:
            _move_to_tail(session, entry)
    else:
        entry = DriverQueue(
            driver_id=driver.id,
            city_id=city_id,
            position=_max_position(session) + 1,
            status="waiting",
        )
        session.add(entry)
    driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def leave_queue(session: Session, driver: User) -> None:
    # Lock the user first.  Queue repair uses the same lock order and therefore
    # cannot recreate a row from stale online flags while departure commits.
    locked_driver = (
        session.query(User)
        .filter(User.id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    if locked_driver is not None:
        driver = locked_driver
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    # Set both canonical availability flags inside the same helper.  Previously
    # some callers set is_on_line only after the queue row was deleted, leaving
    # a short race in which repair/selection could resurrect the driver.
    driver.is_on_line = False
    driver.driver_status = "offline"
    if entry is not None:
        # Cancel a pending/sent «Вы первый в очереди» notice before deleting the
        # row that stores its outbox id.  Otherwise the message could arrive
        # after the driver had already left the line.
        _clear_front_notice(session, entry)
        session.delete(entry)
    session.flush()
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def set_away(session: Session, driver: User) -> None:
    """Mark the driver as temporarily away. Position is kept."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "away"
    driver.driver_status = "away"
    _sync_away_notice(session, driver)
    from . import driver_away_timeout_service
    driver_away_timeout_service.schedule(driver.id)
    _notify_fronts(session)


def return_from_away(session: Session, driver: User) -> DriverQueue:
    """Return from a temporary absence as a free driver at the queue tail."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry is None:
        return join_queue(session, driver, None)
    entry.status = "waiting"
    _move_to_tail(session, entry)
    driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def mark_assigned(session: Session, driver: User) -> None:
    """Driver took an order. Position kept until they finish."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "assigned"
    driver.driver_status = "busy"
    # SessionLocal has autoflush disabled. Persist the assignment before any
    # dispatch/queue queries run in this transaction; otherwise they can still
    # see this driver as waiting/online and issue a second order.
    session.flush()
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def mark_offered(session: Session, driver: User) -> bool:
    """Atomically reserve a driver while exactly one offer awaits a response.

    SessionLocal disables autoflush, so the explicit flush is essential: any
    dispatch query later in this transaction must see ``offered`` instead of
    selecting the same driver again.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if not entry or entry.status != "waiting":
        return False
    entry.status = "offered"
    session.flush()
    _notify_fronts(session)
    return True


def mark_considering(session: Session, driver: User) -> DriverQueue:
    """Reserve a driver during a customer-price negotiation.

    Unlike ``assigned``, this status is shown as «рассматривает заявку». The
    row is still unavailable to FIFO dispatch, so the driver cannot receive a
    second request while entering a price or waiting for the passenger.
    """
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    if entry is None:
        entry = DriverQueue(
            driver_id=driver.id,
            city_id=None,
            position=_max_position(session) + 1,
            status="offered",
        )
        session.add(entry)
    else:
        entry.status = "offered"
    driver.is_on_line = True
    driver.driver_status = "online"
    session.flush()
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def offer_lock_is_live(session: Session, driver: User) -> bool:
    """True while the queue really reserves this driver for a pending offer.

    An order row may still point at a driver after a crash or a lost timer.
    The queue entry is the authoritative reservation, so a claim in the
    requests chat must not be blocked by a dead lock.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    return bool(entry and entry.status == "offered")


def release_offer(session: Session, driver: User) -> None:
    """Make a driver free again after declining/cancelling a pending offer."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry and entry.status == "offered":
        entry.status = "waiting"
    if driver.driver_status != "away":
        driver.driver_status = "online"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def return_to_queue(session: Session, driver: User) -> None:
    """After finishing a ride, put the driver back at the TAIL as free."""
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "waiting"
        _move_to_tail(session, entry)
        driver.driver_status = "online"
    else:
        # Driver was removed from the line meanwhile; just mark online-less.
        driver.driver_status = "offline"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def move_to_tail(session: Session, driver: User) -> None:
    """Explicitly send a (still waiting) driver to the tail. Used when a driver
    declines a regular order and therefore loses their spot.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry and entry.status == "waiting":
        _move_to_tail(session, entry)
    _notify_fronts(session)


def restore_position(session: Session, driver: User) -> None:
    """Return the driver to the free queue KEEPING their current position.

    Used when an order is voided through no fault of the driver — the passenger
    cancelled the trip (within 2 minutes) or it became a false call — so the
    driver must NOT lose their spot and drop to the tail.
    """
    entry = session.query(DriverQueue).filter(DriverQueue.driver_id == driver.id).one_or_none()
    if entry:
        entry.status = "waiting"
        driver.driver_status = "online"
    else:
        driver.driver_status = "offline"
    _sync_away_notice(session, driver)
    _notify_fronts(session)


def move_to_tail_after_success(session: Session, driver: User) -> DriverQueue | None:
    """Return a driver at the tail after a successfully completed request."""
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    if entry is None:
        current_line = (driver.current_line or "").strip().casefold()
        city = next(
            (
                item for item in session.query(City).filter(
                    City.is_active.is_(True)
                ).all()
                if (item.name or "").strip().casefold() == current_line
            ),
            None,
        )
        if city is None:
            driver.is_on_line = False
            driver.driver_status = "offline"
            return None
        entry = DriverQueue(
            driver_id=driver.id,
            city_id=city.id,
            position=_max_position(session) + 1,
            status="waiting",
        )
        session.add(entry)
    else:
        entry.status = "waiting"
        _move_to_tail(session, entry)
    driver.is_on_line = True
    driver.driver_status = "online"
    session.flush()
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def move_to_front(session: Session, driver: User) -> DriverQueue | None:
    """Return a driver as the first free driver without reordering anybody else.

    Used when the passenger asked the driver to cancel: the cancellation is not
    the driver's fault, so their next queue position is guaranteed to be #1.
    """
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    if entry is None:
        current_line = (driver.current_line or "").strip().casefold()
        city = next(
            (
                item for item in session.query(City).filter(
                    City.is_active.is_(True)
                ).all()
                if (item.name or "").strip().casefold() == current_line
            ),
            None,
        )
        if city is None:
            driver.is_on_line = False
            driver.driver_status = "offline"
            return None
        entry = DriverQueue(
            driver_id=driver.id,
            city_id=city.id,
            position=_min_position(session) - 1,
            status="waiting",
        )
        session.add(entry)
    else:
        entry.status = "waiting"
        entry.position = _min_position(session) - 1
    driver.is_on_line = True
    driver.driver_status = "online"
    # Flush the cancelled order and the queue transition together.  Front
    # notification queries must no longer see this driver as busy.
    session.flush()
    _sync_away_notice(session, driver)
    _notify_fronts(session)
    return entry


def _repair_visibly_free_drivers(session: Session) -> None:
    """Make queue rows agree with drivers shown as «Свободен».

    Historical failures could leave users.driver_status='online' while the
    queue row was absent or still marked assigned/offered. The passenger list
    then showed a free driver, but dispatch could not select that driver.
    Repair only drivers with no active ride and no live offer; post-ride line
    choice remains safe because those drivers have status='away'.
    """
    candidates = (
        session.query(User)
        .filter(User.is_on_line.is_(True), User.driver_status == "online")
        .with_for_update(skip_locked=True)
        .all()
    )
    if not candidates:
        return
    candidate_ids = [driver.id for driver in candidates]
    active_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.driver_id).filter(
            Order.driver_id.in_(candidate_ids),
            Order.status.in_(("assigned", "arrived", "in_progress", "parallel_assigned")),
        ).all()
        if driver_id is not None
    }
    parallel_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.parallel_driver_id).filter(
            Order.parallel_driver_id.in_(candidate_ids),
            Order.status == "parallel_assigned",
        ).all()
        if driver_id is not None
    }
    active_driver_ids.update(parallel_driver_ids)
    offered_driver_ids = {
        driver_id for (driver_id,) in session.query(Order.offered_driver_id).filter(
            Order.offered_driver_id.in_(candidate_ids),
            Order.status == "searching",
        ).all()
        if driver_id is not None
    }
    # A passenger-selected driver is also reserved before the order text
    # exists. Do not let the legacy repair turn that 90-second hold back into
    # a free queue row and hand the same driver to an ordinary FIFO request.
    offered_driver_ids.update(
        driver_id for (driver_id,) in session.query(DriverSelection.driver_id).filter(
            DriverSelection.driver_id.in_(candidate_ids)
        ).all()
        if driver_id is not None
    )
    queue_by_driver_id = {
        entry.driver_id: entry
        for entry in session.query(DriverQueue).filter(
            DriverQueue.driver_id.in_(candidate_ids)
        ).all()
    }
    changed = False
    next_position = _max_position(session)
    cities_by_name = {
        (city.name or "").strip().casefold(): city
        for city in session.query(City).filter(City.is_active.is_(True)).all()
    }
    for driver in candidates:
        if not driver.has_role(ROLE_DRIVER):
            continue
        if driver.id in active_driver_ids or driver.id in offered_driver_ids:
            continue
        entry = queue_by_driver_id.get(driver.id)
        city = cities_by_name.get((driver.current_line or "").strip().casefold())
        if entry is None:
            next_position += 1
            session.add(DriverQueue(
                driver_id=driver.id,
                city_id=city.id if city else None,
                position=next_position,
                status="waiting",
            ))
            changed = True
        else:
            city_id = city.id if city else entry.city_id
            if entry.status != "waiting" or entry.city_id != city_id:
                entry.status = "waiting"
                entry.city_id = city_id
                changed = True
    if changed:
        session.flush()


def next_waiting_driver(
    session: Session,
    pickup_city: str | None,
    exclude_driver_ids: list[int] | None = None,
    line_scope: str = "normal",
) -> User | None:
    """Atomically lock and return one eligible free driver.

    ``LIMIT 1 FOR UPDATE SKIP LOCKED`` allows concurrent order workers to pick
    different drivers without locking the whole free-driver queue.
    """
    exclude = set(exclude_driver_ids or [])
    _repair_visibly_free_drivers(session)

    # Drivers on priority lines receive only requests recognized as their own
    # line. Горнозаводск is the
    # universal line: after switching to it, a driver can receive any request.
    aliases = {
        "горнозаводск": "горнозаводск",
        "горнозаводска": "горнозаводск",
        "горнозаводске": "горнозаводск",
        "пашия": "пашия", "пашии": "пашия",
        "кусья": "кусья", "кусьи": "кусья",
        "чусовой": "чусовой", "чусового": "чусовой",
        "чус": "чусовой", "чуса": "чусовой",
        "сараны": "сараны", "саранов": "сараны",
        "тёплая гора": "сараны", "теплая гора": "сараны",
    }
    pickup = aliases.get((pickup_city or "").strip().casefold())

    # Resolve configured line names in Python and filter by the canonical
    # drivers_queue.city_id. This removes locale-dependent PostgreSQL LOWER /
    # correlated-subquery behavior from the critical dispatch path.
    city_ids_by_name = {
        (city.name or "").strip().casefold(): city.id
        for city in session.query(City).filter(City.is_active.is_(True)).all()
    }
    if line_scope == "exact":
        eligible_names = {pickup} if pickup else set()
    elif line_scope == "all":
        # Never cross the two village lines. Even legacy callers requesting
        # broad fallback may use only the matching village plus Gornozavodsk.
        eligible_names = ({pickup, "горнозаводск"} if pickup else {"горнозаводск"})
    else:
        eligible_names = ({pickup, "горнозаводск"} if pickup else {"горнозаводск"})
    eligible_city_ids = [
        city_ids_by_name[name] for name in eligible_names
        if name in city_ids_by_name
    ]
    if not eligible_city_ids:
        return None
    query = (
        session.query(DriverQueue)
        .join(User, User.id == DriverQueue.driver_id)
        .filter(
            DriverQueue.status == "waiting",
            DriverQueue.city_id.in_(eligible_city_ids),
            User.is_on_line.is_(True),
            User.driver_status == "online",
            User.is_blocked.is_(False),
            ~DriverQueue.driver_id.in_(
                session.query(Order.offered_driver_id).filter(
                    Order.status == "searching",
                    Order.offered_driver_id.isnot(None),
                )
            ),
            ~DriverQueue.driver_id.in_(
                session.query(Order.driver_id).filter(
                    Order.status.in_(("assigned", "arrived", "in_progress", "parallel_assigned")),
                    Order.driver_id.isnot(None),
                )
            ),
            ~DriverQueue.driver_id.in_(
                session.query(Order.parallel_driver_id).filter(
                    Order.status == "parallel_assigned",
                    Order.parallel_driver_id.isnot(None),
                )
            ),
        )
    )
    if exclude:
        query = query.filter(DriverQueue.driver_id.notin_(exclude))
    entry = (
        query.order_by(DriverQueue.position.asc(), DriverQueue.id.asc())
        .with_for_update(skip_locked=True, of=DriverQueue)
        .first()
    )
    if not entry:
        return None
    # The queue row is the canonical availability record. Repair duplicated
    # display flags left out of sync by older releases.
    driver = entry.driver
    driver.is_on_line = True
    driver.driver_status = "online"
    return driver

def has_waiting_driver(
    session: Session,
    pickup_city: str | None = None,
    exclude_driver_ids: list[int] | None = None,
    line_scope: str = "normal",
) -> bool:
    """Whether dispatch can find an eligible driver for this specific order."""
    return next_waiting_driver(
        session,
        pickup_city,
        exclude_driver_ids=exclude_driver_ids,
        line_scope=line_scope,
    ) is not None


def queue_entries(session: Session) -> list[dict]:
    """Return all drivers currently in the queue, sorted free -> away -> busy,
    each in queue (position) order. Items: {driver, status, position}.
    """
    rows = session.query(DriverQueue).all()
    drivers = [session.get(User, entry.driver_id) for entry in rows]
    live_statuses = actual_driver_statuses(
        session, [driver for driver in drivers if driver is not None]
    )
    items = []
    for entry, driver in zip(rows, drivers):
        if driver is None:
            continue
        live_status = live_statuses.get(driver.id, "offline")
        if live_status == "busy":
            effective_status = "assigned"
        elif live_status == "away":
            effective_status = "away"
        elif live_status == "offline":
            effective_status = "offline"
        elif live_status == "considering" or entry.status == "offered":
            effective_status = "offered"
        else:
            effective_status = "waiting"
        items.append({
            "driver": driver,
            "status": effective_status,
            "position": entry.position,
            "city_id": entry.city_id,
            "queue_id": entry.id,
            "live_status": live_status,
        })
    items.sort(key=lambda it: (_QUEUE_STATUS_ORDER.get(it["status"], 9), it["position"], it["queue_id"]))
    return items


def free_queue_snapshot(session: Session, entries: list[dict] | None = None) -> list[dict]:
    """Free queue in the same order/status snapshot used by the visible list."""
    rows = queue_entries(session) if entries is None else entries
    return [item for item in rows if item["status"] == "waiting"
            and item["driver"].is_on_line
            and item["driver"].driver_status == "online"
            and not item["driver"].is_blocked
            and item["driver"].has_role(ROLE_DRIVER)]


def driver_queue_rank(session: Session, driver: User, entries: list[dict] | None = None) -> int | None:
    """Rank among actually free drivers, not raw waiting queue rows."""
    if (not driver.is_on_line or driver.driver_status != "online"
            or _driver_has_active_work(session, driver.id)):
        return None
    for rank, item in enumerate(free_queue_snapshot(session, entries), 1):
        if item["driver"].id == driver.id:
            return rank
    return None


def driver_line_rank(session: Session, driver: User, entries: list[dict] | None = None) -> int | None:
    """Rank among actually free drivers on this driver's canonical line."""
    if (not driver.is_on_line or driver.driver_status != "online"
            or _driver_has_active_work(session, driver.id)):
        return None
    free = free_queue_snapshot(session, entries)
    own = next((item for item in free if item["driver"].id == driver.id), None)
    if own is None:
        return None
    same_line = [item for item in free if item["city_id"] == own["city_id"]]
    return next((rank for rank, item in enumerate(same_line, 1)
                 if item["driver"].id == driver.id), None)


def free_drivers(session: Session) -> list[User]:
    """Users who are drivers and currently free (waiting), in queue order."""
    waiting = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting")
        .order_by(DriverQueue.position.asc(), DriverQueue.id.asc())
        .all()
    )
    result: list[User] = []
    for entry in waiting:
        driver = session.get(User, entry.driver_id)
        if (
            driver
            and driver.has_role(ROLE_DRIVER)
            and not driver.is_blocked
            and driver.is_on_line
            and driver.driver_status == "online"
            and not _driver_has_active_work(session, driver.id)
        ):
            result.append(driver)
    return result


def free_drivers_on_line(session: Session, city_id: int | None) -> list[User]:
    """Free drivers currently on a specific line (req 4)."""
    if not city_id:
        return []
    waiting = (
        session.query(DriverQueue)
        .filter(DriverQueue.status == "waiting", DriverQueue.city_id == city_id)
        .order_by(DriverQueue.position.asc(), DriverQueue.id.asc())
        .all()
    )
    result: list[User] = []
    for entry in waiting:
        driver = session.get(User, entry.driver_id)
        if (
            driver
            and driver.has_role(ROLE_DRIVER)
            and not driver.is_blocked
            and driver.is_on_line
            and driver.driver_status == "online"
            and not _driver_has_active_work(session, driver.id)
        ):
            result.append(driver)
    return result


def all_drivers(session: Session) -> list[User]:
    """Every user that has the driver role, sorted free -> away -> busy -> offline."""
    drivers = session.query(User).filter(
        (User.granted_roles == ROLE_DRIVER)
        | (User.granted_roles.like("driver,%"))
        | (User.granted_roles.like("%,driver"))
        | (User.granted_roles.like("%,driver,%"))
    ).all()
    drivers.sort(key=lambda u: (STATUS_ORDER.get(u.driver_status, 9), (u.full_name or "").lower()))
    return drivers


def actual_driver_statuses(session: Session, drivers: list[User]) -> dict[int, str]:
    """Build live statuses from active orders and canonical queue rows."""
    driver_ids = [driver.id for driver in drivers]
    if not driver_ids:
        return {}
    busy_ids = {
        driver_id
        for (driver_id,) in session.query(Order.driver_id).filter(
            Order.driver_id.in_(driver_ids),
            Order.status.in_(("assigned", "arrived", "in_progress", "parallel_assigned")),
        ).all()
        if driver_id is not None
    }
    busy_ids.update(
        driver_id
        for (driver_id,) in session.query(Order.parallel_driver_id).filter(
            Order.parallel_driver_id.in_(driver_ids),
            Order.status == "parallel_assigned",
        ).all()
        if driver_id is not None
    )
    queue_rows = {
        row.driver_id: row
        for row in session.query(DriverQueue).filter(DriverQueue.driver_id.in_(driver_ids)).all()
    }
    offered_ids = {
        driver_id for (driver_id,) in session.query(Order.offered_driver_id).filter(
            Order.offered_driver_id.in_(driver_ids), Order.status == "searching",
        ).all() if driver_id is not None
    }
    from common import time_utils
    offered_ids.update(
        driver_id for (driver_id,) in session.query(DriverSelection.driver_id).filter(
            DriverSelection.driver_id.in_(driver_ids),
            DriverSelection.expires_at > time_utils.now(),
        ).all() if driver_id is not None
    )
    result: dict[int, str] = {}
    for driver in drivers:
        row = queue_rows.get(driver.id)
        if driver.id in busy_ids or (row and row.status == "assigned") or driver.driver_status == "busy":
            result[driver.id] = "busy"
        elif (not driver.is_on_line or driver.driver_status == "offline"
              or driver.is_blocked or not driver.has_role(ROLE_DRIVER)):
            result[driver.id] = "offline"
        elif driver.driver_status == "away" or (row and row.status == "away"):
            result[driver.id] = "away"
        elif driver.id in offered_ids or (row and row.status == "offered"):
            result[driver.id] = "considering"
        elif row and row.status == "waiting":
            result[driver.id] = "online"
        elif row and row.status == "away":
            result[driver.id] = "away"
        else:
            result[driver.id] = "offline"
    return result



def _line_has_waiting_assignment(session: Session, city_id: int | None) -> bool:
    """Whether this line already has an unassigned order ready for dispatch.

    A driver joining such a line is about to receive/compete for the waiting
    request, so sending «Вы первый в очереди» immediately beforehand is noisy
    and misleading. Горнозаводск is the universal fallback line; village
    lines only match requests whose pickup is that same village.
    """
    if not city_id:
        return False
    city = session.get(City, city_id)
    if not city:
        return False
    from . import parallel_orders

    waiting_orders = parallel_orders.available(session)
    if not waiting_orders:
        return False
    line_name = (city.name or "").strip().casefold()
    if line_name == "горнозаводск":
        return True
    for order in waiting_orders:
        if parallel_orders.is_route_fallback(order):
            pickup_line = "горнозаводск"
        else:
            pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
        if pickup_line.strip().casefold() == line_name:
            return True
    return False


def _driver_has_active_work(session: Session, driver_id: int) -> bool:
    """True when the driver serves a ride, holds a parallel request or is
    reviewing an offer. Such a driver must not be told they are first in
    the queue and that the next request is theirs."""
    busy = (
        session.query(Order.id)
        .filter(
            Order.driver_id == driver_id,
            Order.status.in_((
                "created", "queued", "searching", "chat_search",
                "assigned", "arrived", "in_progress", "parallel_assigned",
            )),
        )
        .first()
    )
    if busy:
        return True
    parallel = (
        session.query(Order.id)
        .filter(
            Order.parallel_driver_id == driver_id,
            Order.status == "parallel_assigned",
        )
        .first()
    )
    if parallel:
        return True
    offered = (
        session.query(Order.id)
        .filter(
            Order.offered_driver_id == driver_id,
            Order.status == "searching",
        )
        .first()
    )
    return bool(offered)


def _clear_front_notice(session: Session, entry) -> None:
    """Drop the flag and delete the previously sent first-in-queue notice."""
    outbox_id = getattr(entry, "front_notice_outbox_id", None)
    entry.front_notified = False
    entry.front_notice_outbox_id = None
    if not outbox_id:
        return
    try:
        from . import outbox_service

        outbox_service.cancel_or_delete(session, int(outbox_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to clear queue-front notice: driver=%s outbox=%s error=%s",
            getattr(entry, "driver_id", None),
            outbox_id,
            exc,
            exc_info=True,
        )


def is_current_queue_front(session: Session, entry: DriverQueue) -> bool:
    city = session.get(City, entry.city_id) if entry.city_id else None
    if not city or (city.name or "").strip().casefold() != "горнозаводск":
        return False
    free = [item for item in free_queue_snapshot(session) if item["city_id"] == entry.city_id]
    return bool(free and free[0]["queue_id"] == entry.id
                and not _driver_has_active_work(session, entry.driver_id)
                and not _line_has_waiting_assignment(session, entry.city_id))


def _notify_fronts(session: Session) -> None:
    """Task 9: notify a driver the moment they become #1 (front) of the free
    queue on their line. Sent once per time thanks to the front_notified flag."""
    try:
        from common.settings_service import msg as _msg
        from .vk_client import vk as _vk
        # SessionLocal disables autoflush: ranks must see this transition,
        # not a previous waiting/offered/assigned row in the database.
        session.flush()
        front_ids = set()
        gorn = session.query(City).filter(City.name == "Горнозаводск").one_or_none()
        gorn_id = gorn.id if gorn else None
        if gorn_id is not None:
            snapshot = free_queue_snapshot(session)
            waiting_ids = [item["queue_id"] for item in snapshot if item["city_id"] == gorn_id]
            all_rows = {entry.id: entry for entry in session.query(DriverQueue).all()}
            waiting = [all_rows[entry_id] for entry_id in waiting_ids if entry_id in all_rows]
            for e in waiting:
                drv = session.get(User, e.driver_id)
                if not drv or drv.is_blocked or not drv.is_on_line or drv.driver_status != "online":
                    continue
                if _driver_has_active_work(session, drv.id):
                    continue
                front_ids.add(e.id)
                break
        for e in session.query(DriverQueue).all():
            if e.id in front_ids:
                # When an order is already waiting on this line, dispatch will
                # follow immediately (or after passenger actuality check).
                # Do not precede it with the generic first-in-queue message.
                if _line_has_waiting_assignment(session, e.city_id):
                    _clear_front_notice(session, e)
                    continue
                if not getattr(e, "front_notified", False):
                    drv = session.get(User, e.driver_id)
                    if drv and _driver_has_active_work(session, drv.id):
                        _clear_front_notice(session, e)
                        continue
                    if drv:
                        try:
                            e.front_notice_outbox_id = _vk.send_tracked_message(
                                drv.vk_id, _msg(session, "msg_queue_first"), priority=901
                            )
                            e.front_notified = bool(e.front_notice_outbox_id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "Failed to queue front-position notice: driver=%s error=%s",
                                getattr(drv, "id", None),
                                exc,
                                exc_info=True,
                            )
            else:
                # The driver is no longer the front of the line (an offer was
                # sent to them, they went on a ride, or somebody who kept their
                # place took the first position back). The notice must go away.
                if getattr(e, "front_notified", False) or getattr(
                    e, "front_notice_outbox_id", None
                ):
                    _clear_front_notice(session, e)
    except Exception as exc:  # noqa: BLE001
        # Queue transitions must remain available if a non-critical notice
        # fails, but the failure must be visible to operators.
        log.error("Queue-front notification refresh failed: %s", exc, exc_info=True)
