"""Race-safe manual order assignment shared by the web administration."""
from __future__ import annotations

from sqlalchemy.orm import Session

from common import audit, time_utils
from common.models import DriverQueue, Order, User

from . import delivery_service, keyboards as kb, order_service, queue_service, timers
from .states_service import States, reset, set_state
from .vk_client import vk


ASSIGNABLE_STATUSES = (
    "created",
    "queued",
    "searching",
    "chat_search",
    "no_drivers",
)
ACTIVE_DRIVER_STATUSES = (
    "assigned",
    "arrived",
    "in_progress",
)


def assign_from_admin(session: Session, order_id: int, driver_id: int) -> bool:
    """Assign one currently free queued driver, or leave everything unchanged.

    Lock order is stable across this operation: order, user, queue row.  The
    method intentionally returns a boolean so the existing web UI can retain
    its current buttons and messages.
    """
    order = (
        session.query(Order)
        .filter(Order.id == int(order_id))
        .with_for_update()
        .one_or_none()
    )
    if order is None or order.status not in ASSIGNABLE_STATUSES:
        return False

    driver = (
        session.query(User)
        .filter(User.id == int(driver_id))
        .with_for_update()
        .one_or_none()
    )
    if (
        driver is None
        or not driver.has_role("driver")
        or driver.is_blocked
        or not driver.is_on_line
        or driver.driver_status != "online"
    ):
        return False

    queue_row = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    if queue_row is None or queue_row.status != "waiting":
        return False

    active = session.query(Order.id).filter(
        Order.id != order.id,
        Order.status.in_(ACTIVE_DRIVER_STATUSES),
        (Order.driver_id == driver.id) | (Order.parallel_driver_id == driver.id),
    ).first()
    if active is not None:
        return False

    previous_offered_id = order.offered_driver_id
    if previous_offered_id and previous_offered_id != driver.id:
        previous = session.get(User, previous_offered_id)
        if previous is not None:
            order_service.finalize_offer_message(
                session,
                order,
                "Заявка передана следующему свободному водителю.",
            )
            queue_service.release_offer(session, previous)
            reset(session, previous.vk_id, States.D_ON_LINE)

    timers.cancel("accept", order.id)
    timers.cancel("driver_chat", order.id)
    timers.cancel("dispatcher_unclaimed", order.id)
    order.offered_driver_id = None
    order.parallel_driver_id = None
    order.driver_id = driver.id
    order.status = "assigned"
    order.driver_accept_time = time_utils.now()
    if order.customer_price is not None and order.agreed_price is None:
        order.agreed_price = order.customer_price
    driver.driver_missed_offers = 0
    queue_service.mark_assigned(session, driver)
    order_service.finalize_chat_order_notice(session, order, driver)
    order_service.notify_driver_found(session, order)
    audit.record(session, "admin_assign_driver", f"order={order.id} driver={driver.id}")

    if delivery_service.is_delivery(order):
        delivery_service.request_price(session, driver, order)
    else:
        set_state(
            session,
            driver.vk_id,
            States.D_IN_RIDE,
            {"order_id": order.id},
            merge=False,
        )
        vk.send_message(
            driver.vk_id,
            f"✅ Вы взяли заявку #{order.number} ({order_service.order_type_label(order)})\n"
            f"Ваша заявка: {order_service.order_text(order)}",
            keyboard=kb.driver_ride_keyboard(
                "assigned",
                driver_gender=driver.driver_gender,
            ),
        )
    return True