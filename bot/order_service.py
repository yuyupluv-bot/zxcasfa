"""High-level order lifecycle helpers shared by handlers and timers."""
from __future__ import annotations

import datetime as dt
import json
import re

from sqlalchemy.orm import Session

from common import audit
from common import bot_messages_service as bm
from common.logger import get_logger
from common.models import DriverQueue, DriverSelection, Order, OutboxMessage, User
from common.settings_service import get_float, get_int, msg, set_setting

from . import keyboards as kb
from . import queue_service, timers, waiting_service
from .states_service import States, get_data, reset, set_state
from .vk_client import vk

log = get_logger("bot.orders")

# Every request published to the unified driver chat lives exactly 45 minutes.
CHAT_UNCLAIMED_TIMEOUT_SECONDS = 45 * 60
# A passenger who named a fare expects a quick answer and may change that fare
# while the same ten-minute window is running.
CUSTOMER_PRICE_UNCLAIMED_TIMEOUT_SECONDS = 10 * 60


def dispatcher_driver_details(driver: User) -> str:
    """Driver identity required in every dispatcher lifecycle update."""
    name = driver.full_name or f"id{driver.vk_id}"
    car = " ".join(value for value in (driver.car_model, driver.car_color) if value) or "не указана"
    number = driver.car_number or "не указан"
    return f"Водитель: {name}\nМашина: {car}\nНомер машины: {number}"


DELIVERY_TYPES = ("delivery", "partner_delivery")


def is_delivery_type(order: Order) -> bool:
    """Both delivery kinds: driver-priced delivery and partner delivery."""
    return order.order_type in DELIVERY_TYPES


def order_type_label(order: Order) -> str:
    if order.order_type == "partner_delivery":
        return "📦 Доставка от партнёров"
    return "📦 Доставка" if order.order_type == "delivery" else "🚕 Обычная"


def order_text(order: Order) -> str:
    """Return the passenger's original text verbatim, without separators."""
    return (order.route_text or order.address_to or order.address_from or "").strip()


def multi_car_label(session: Session, order: Order) -> str:
    """Return a stable «Машина 1 из 2» label for a grouped request."""
    if not order.multi_car_group_id:
        return ""
    rows = session.query(Order).filter(
        Order.multi_car_group_id == order.multi_car_group_id
    ).order_by(Order.id.asc()).all()
    if len(rows) < 2:
        return ""
    ids = [row.id for row in rows]
    try:
        number = ids.index(order.id) + 1
    except ValueError:
        return ""
    return f"Машина {number} из {len(rows)}"


def _passenger_review_word(count: int) -> str:
    count = abs(int(count))
    if 10 <= count % 100 <= 20:
        return "отзывов"
    if count % 10 == 1:
        return "отзыв"
    if 2 <= count % 10 <= 4:
        return "отзыва"
    return "отзывов"


def passenger_rating_text(passenger: User) -> str:
    """Passenger rating shown to a driver on every ordinary offer card."""
    count = int(passenger.passenger_rating_count or 0)
    if not count:
        return "— ⭐ (нет отзывов)"
    return f"{passenger.passenger_rating:.1f} ⭐ ({count} {_passenger_review_word(count)})"


def driver_rating_text(driver: User) -> str:
    """Driver rating shown to a passenger as ``5.0 ⭐ (67 отзывов)``."""
    count = int(driver.rating_count or 0)
    if not count:
        return "— ⭐ (нет отзывов)"
    return f"{driver.rating:.1f} ⭐ ({count} {_passenger_review_word(count)})"


def driver_card_text(driver: User) -> str:
    """One canonical passenger-facing driver card used at accept and ETA."""
    name = driver.full_name or ("id" + str(driver.vk_id))
    car = ", ".join(
        value
        for value in (driver.car_model, driver.car_color, driver.car_number)
        if value
    ) or "не указано"
    return (
        f"[id{driver.vk_id}|{name}] {driver_rating_text(driver)}\n"
        f"Авто: {car}."
    )


def departure_driver_card_text(driver: User) -> str:
    """Passenger ETA card with average, star and review count."""
    name = driver.full_name or ("id" + str(driver.vk_id))
    rating = driver_rating_text(driver)
    car = ", ".join(
        value for value in (driver.car_model, driver.car_color, driver.car_number)
        if value
    ) or "не указано"
    return f"[id{driver.vk_id}|{name}] {rating}\nАвто: {car}"


def driver_chat_reason_label(session: Session, order: Order) -> str | None:
    """Human-readable category for an order published into the driver chat."""
    reasons = _declined_reasons(session, order)
    if order.customer_price is not None:
        return "заявку с указанной ценой"
    if is_delivery_type(order) or "delivery" in reasons:
        return "доставку"
    if "far" in reasons:
        return "дальнее расстояние"
    return None


def normalize_driver_chat_peer_id(value: int) -> int:
    """Accept either a VK conversation id or a full peer_id.

    The VK API addresses chats as 2_000_000_000 + conversation_id. Admins
    commonly paste only the short conversation id, so normalize it here.
    """
    if value <= 0:
        return 0
    if value < 2_000_000_000:
        return 2_000_000_000 + value
    return value


def send_driver_chat_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> bool:
    """Backward-compatible name: all driver notices now go to the requests chat."""
    return send_fallback_chat_notice(session, text, keyboard=keyboard, attachment=attachment)


def send_fallback_chat_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> bool:
    """Send bookings and fallback requests to the unified requests chat."""
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    if not peer_id:
        log.error("Fallback chat is not configured")
        return False
    sent = vk.send_message(peer_id, text, keyboard=keyboard, attachment=attachment)
    if not sent:
        log.error("Could not queue fallback-chat message peer_id=%s", peer_id)
    return sent



def send_fallback_chat_tracked_notice(
    session: Session,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
) -> int | None:
    """Queue a shared-chat request card and return its outbox id."""
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    if not peer_id:
        log.error("Fallback chat is not configured")
        return None
    outbox_id = vk.send_tracked_message(
        peer_id,
        text,
        keyboard=keyboard,
        attachment=attachment,
    )
    if not outbox_id:
        log.error("Could not queue tracked fallback request peer_id=%s", peer_id)
    return outbox_id


def publish_direct_to_requests_chat(session: Session, order: Order) -> bool:
    """Publish dispatcher and passenger-priced orders without FIFO dispatch."""
    creator = session.get(User, order.passenger_id)
    if not creator:
        log.error("Could not publish direct-chat order=%s: creator not found", order.id)
        order.status = "no_drivers"
        return False
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    if not peer_id:
        log.error("Could not publish direct-chat order=%s: requests chat is not configured", order.id)
        order.status = "no_drivers"
        vk.send_message(
            creator.vk_id,
            "Не удалось отправить заявку в водительский чат. "
            "Проверьте настройку driver_fallback_chat_peer_id.",
        )
        return False
    order.status = "chat_search"
    prepared_voice = vk.prepare_voice_attachment(peer_id, order.voice_attachment)
    requester_block = (
        "🎧 Заявка от диспетчера"
        if order.dispatcher_id
        else f"Пассажир: [id{creator.vk_id}|{creator.full_name or ('id' + str(creator.vk_id))}]"
    )
    price_line = (
        f"💰 Цена клиента: {float(order.customer_price):.0f} ₽\n"
        if order.customer_price is not None else ""
    )
    order.chat_notice_outbox_id = send_fallback_chat_tracked_notice(
        session,
        f"@all\n🔔 Заявка №{order.number}\n"
        f"Маршрут: {order_text(order)}\n"
        f"{price_line}{requester_block}",
        keyboard=kb.chat_take_keyboard(order.id, order.number),
        attachment=prepared_voice,
    )
    if not order.chat_notice_outbox_id:
        order.status = "no_drivers"
        vk.send_message(
            creator.vk_id,
            "Не удалось отправить заявку в водительский чат. "
            "Проверьте настройку driver_fallback_chat_peer_id.",
        )
        return False
    direct_chat_timeout = (
        CUSTOMER_PRICE_UNCLAIMED_TIMEOUT_SECONDS
        if order.customer_price is not None and not order.dispatcher_id
        else CHAT_UNCLAIMED_TIMEOUT_SECONDS
    )
    timers.schedule(
        "driver_chat", order.id, direct_chat_timeout,
        lambda: _driver_chat_timeout(order.id),
    )
    audit.record(session, "direct_chat_publish", f"order={order.id}")
    return True


def replace_customer_price_chat_notice(
    session: Session,
    order: Order,
    old_price: int,
    new_price: int,
) -> bool:
    """Atomically queue a changed-price card and retire the previous card.

    The existing expiry timer is deliberately left untouched: changing the
    fare must not extend the original ten-minute lifetime of the request.
    """
    creator = session.get(User, order.passenger_id)
    peer_id = normalize_driver_chat_peer_id(
        get_int(session, "driver_fallback_chat_peer_id", 0)
    )
    old_outbox_id = order.chat_notice_outbox_id
    if not creator or not peer_id or not old_outbox_id:
        log.error(
            "Could not replace customer-price card order=%s creator=%s peer=%s outbox=%s",
            order.id, bool(creator), peer_id, old_outbox_id,
        )
        return False

    prepared_voice = vk.prepare_voice_attachment(peer_id, order.voice_attachment)
    requester_block = (
        f"Пассажир: [id{creator.vk_id}|"
        f"{creator.full_name or ('id' + str(creator.vk_id))}]"
    )
    new_outbox_id = send_fallback_chat_tracked_notice(
        session,
        f"Клиент изменил цену ({old_price} руб. → {new_price} руб.)\n"
        f"🔔 Заявка №{order.number}\n"
        f"Маршрут: {order_text(order)}\n"
        f"💰 Цена клиента: {new_price} ₽\n"
        f"{requester_block}",
        keyboard=kb.chat_take_keyboard(order.id, order.number),
        attachment=prepared_voice,
    )
    if not new_outbox_id:
        return False

    from . import outbox_service
    if not outbox_service.cancel_or_delete(session, old_outbox_id):
        # Do not leave a second live card if the original cannot be retired.
        outbox_service.cancel_or_delete(session, new_outbox_id)
        return False

    order.chat_notice_outbox_id = new_outbox_id
    return True


def notify_driver_found(session: Session, order: Order) -> None:
    """No intermediate passenger notice; retire legacy acceptance cards."""
    outbox_id = getattr(order, "offer_notice_outbox_id", None)
    if outbox_id:
        from . import outbox_service
        if outbox_service.cancel_or_delete(session, outbox_id):
            order.offer_notice_outbox_id = None


def clear_offer_notice(session: Session, order: Order) -> None:
    """Delete passenger notices that belong to the previous driver offer.

    Both «Нашёлся свободный водитель» and «Водитель указывает время…»
    describe one specific driver. When the request is re-offered they
    must disappear instead of stacking up in the passenger's chat.
    """
    from . import outbox_service
    for field in ("search_notice_outbox_id", "offer_notice_outbox_id"):
        outbox_id = getattr(order, field, None)
        if not outbox_id:
            continue
        if outbox_service.cancel_or_delete(session, outbox_id):
            setattr(order, field, None)


def finalize_offer_message(session: Session, order: Order, result_text: str) -> bool:
    """Remove the Поехать/Отказаться buttons from the driver's offer card."""
    outbox_id = order.offer_outbox_id
    if not outbox_id:
        return False
    from . import outbox_service
    reusable_voice = order.voice_attachment
    if reusable_voice and str(reusable_voice).lstrip().startswith("{"):
        reusable_voice = None
    finalized = outbox_service.finalize_tracked_message(
        session,
        outbox_id,
        result_text,
        attachment=reusable_voice,
    )
    if finalized:
        order.offer_outbox_id = None
    return finalized


def delete_chat_order_notice(session: Session, order: Order) -> bool:
    """Delete the request card for everyone after a final chat action."""
    outbox_id = order.chat_notice_outbox_id
    if not outbox_id:
        return True
    from . import outbox_service
    deleted_or_cancelled = outbox_service.cancel_or_delete(session, outbox_id)
    if deleted_or_cancelled:
        order.chat_notice_outbox_id = None
        log.info("Chat request notice removed order=%s outbox=%s", order.id, outbox_id)
    else:
        log.warning("Chat request notice deletion pending order=%s outbox=%s", order.id, outbox_id)
    return deleted_or_cancelled


def finalize_chat_order_notice(
    session: Session,
    order: Order,
    driver: User,
    *,
    considering_price: bool = False,
) -> bool:
    """Turn the original shared request card into a final buttonless notice."""
    name = driver.full_name or ("id" + str(driver.vk_id))
    mention = f"[id{driver.vk_id}|{name}]"
    # Legacy wording retained for release-test compatibility: «Заявку взял водитель:».
    text = (
        f"💬 Заявку #{order.number} рассматривает водитель {mention} и предлагает свою цену."
        if considering_price else
        f"✅ Заявку #{order.number} взял водитель {mention}"
    )
    outbox_id = order.chat_notice_outbox_id
    if not outbox_id:
        return send_fallback_chat_notice(session, text)
    from . import outbox_service
    if outbox_service.finalize_tracked_message(session, outbox_id, text):
        order.chat_notice_outbox_id = None
        log.info("Chat request notice finalized order=%s outbox=%s", order.id, outbox_id)
        return True
    outbox_service.cancel_or_delete(session, outbox_id)
    order.chat_notice_outbox_id = None
    return send_fallback_chat_notice(session, text)


def publish_special_decline_to_requests_chat(session: Session, order: Order) -> bool:
    """Publish a delivery/far order immediately after the first such refusal.

    It must not be offered to the next FIFO driver: drivers in the requests
    chat decide together whether someone will take it.
    """
    creator = session.get(User, order.passenger_id)
    reasons = _declined_reasons(session, order)
    reason_label = ("Маленькая стоимость" if "low_price" in reasons else
                    "Доставка" if is_delivery_type(order) or "delivery" in reasons else
                    "Дальнее расстояние")
    chat_handoff = getattr(order, "last_decline_reason", None) == "to_driver_chat"
    if chat_handoff:
        reason_label = "Отдать в водительскую"
    peer_id = normalize_driver_chat_peer_id(get_int(session, "driver_fallback_chat_peer_id", 0))
    if not creator or not peer_id:
        log.error("Requests chat is not configured for special decline order=%s", order.id)
        return False
    order.status = "chat_search"
    # Voice requests store their original VK voice metadata. Re-upload it for
    # the unified requests-chat peer so VK receives a playable audio message,
    # not only the textual route summary.
    prepared_voice = vk.prepare_voice_attachment(peer_id, order.voice_attachment)
    if order.dispatcher_id:
        requester_block = "🎧 Заявка от диспетчера"
    else:
        requester_block = (
            f"Пассажир: [id{creator.vk_id}|"
            f"{creator.full_name or ('id' + str(creator.vk_id))}]"
        )
    order.chat_notice_outbox_id = send_fallback_chat_tracked_notice(
        session,
        ("@all\n" if chat_handoff else "") +
        f"🔔 Заявка №{order.number}\nПричина: {reason_label}\n"
        f"Маршрут: {order_text(order)}\n"
        + (f"💰 Цена клиента: {float(order.customer_price):.0f} ₽\n" if order.customer_price is not None else "")
        + f"{requester_block}",
        keyboard=kb.chat_take_keyboard(order.id, order.number),
        attachment=prepared_voice,
    )
    if not order.chat_notice_outbox_id:
        order.status = "no_drivers"
        return False
    vk.send_message(
        creator.vk_id,
        ("✅ Активно ищем водителя в чате, ожидайте"
         if chat_handoff else
         "Заявка сразу отправлена в чат заявок. Ожидаем, согласится ли водитель."),
        keyboard=kb.passenger_waiting_keyboard(order.id),
    )
    if chat_handoff and not order.dispatcher_id:
        set_state(session, creator.vk_id, States.P_WAITING, {"order_id": order.id}, merge=False)
    timers.schedule(
        "driver_chat", order.id, CHAT_UNCLAIMED_TIMEOUT_SECONDS,
        lambda: _driver_chat_timeout(order.id),
    )
    return True

def has_eligible_waiting_driver(session: Session, order: Order) -> bool:
    """Check availability excluding drivers who already declined this order."""
    from . import parallel_orders
    route_city = parallel_orders.route_priority_city(order)
    if route_city:
        pickup_line = (
            "Горнозаводск"
            if parallel_orders.is_route_fallback(order)
            else route_city
        )
        line_scope = "exact"
    else:
        pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
        line_scope = "normal"
    return queue_service.has_waiting_driver(
        session,
        pickup_line,
        exclude_driver_ids=_declined_ids(session, order),
        line_scope=line_scope,
    )


def offer_to_selected_driver(session: Session, order: Order, driver: User) -> bool:
    """Offer a new request only to the driver explicitly selected by its passenger."""
    locked_order = (
        session.query(Order)
        .filter(Order.id == order.id)
        .with_for_update()
        .one_or_none()
    )
    entry = (
        session.query(DriverQueue)
        .filter(DriverQueue.driver_id == driver.id)
        .with_for_update()
        .one_or_none()
    )
    selection = (
        session.query(DriverSelection)
        .filter(
            DriverSelection.driver_id == driver.id,
            DriverSelection.passenger_id == order.passenger_id,
        )
        .with_for_update()
        .one_or_none()
    )
    selection_live = bool(selection and selection.expires_at > dt.datetime.now(dt.timezone.utc))
    if (
        not locked_order
        or not selection_live
        or locked_order.requested_driver_id != driver.id
        or locked_order.driver_id is not None
        or locked_order.parallel_driver_id is not None
        or locked_order.status in (
            "assigned", "arrived", "in_progress", "completed", "cancelled",
        )
        or not entry or entry.status not in ("offered", "waiting")
        or not driver.is_on_line or driver.driver_status != "online"
        or driver.is_blocked
        or queue_service._driver_has_active_work(session, driver.id)
    ):
        log.warning("Direct offer rejected: order=%s driver=%s selection_live=%s queue=%s status=%s",
                    order.id, driver.id, selection_live, entry.status if entry else None, driver.driver_status)
        return False
    order = locked_order
    from . import parallel_orders
    if not parallel_orders.local_line_allows(order, driver):
        return False
    entry.status = "offered"
    order.status = "searching"
    _set_current_offer(session, order, driver.id)
    session.flush()
    from . import passenger_queue
    passenger_queue.pause_actuality_prompts(session, except_order_id=order.id)
    log.info(
        "Direct offer: order=%s selected_driver=%s queue reservation=offered",
        order.id,
        driver.id,
    )

    text = (
        f"🔔 Персональная заявка #{order.number} ({order_type_label(order)})\n"
        f"Пассажир выбрал вас.\nВаша заявка: {order_text(order)}"
    )
    if order.customer_price is not None:
        text += f"\n💰 Цена клиента: {float(order.customer_price):.0f} ₽"
    creator = session.get(User, order.passenger_id)
    if creator:
        full_name = creator.full_name or ("id" + str(creator.vk_id))
        text += "\n👤 От кого: [id%s|%s] %s" % (
            creator.vk_id, full_name, passenger_rating_text(creator)
        )
    if order.actuality_confirmed:
        text += "\n✅ Клиент подтвердил актуальность заявки."
    if order.comment:
        text += f"\n💬 Комментарий: {order.comment}"
    text += _extras_summary(session, order)
    if order.night_surcharge:
        night_amount = get_int(session, "night_surcharge_amount", 50)
        text += f"\n🌙 Ночной тариф: +{night_amount} ₽ (23:00–06:00)"
    if is_delivery_type(order):
        text += "\nℹ️ При отказе от доставки водитель остаётся на своём месте в очереди."
    prepared_voice = vk.prepare_voice_attachment(driver.vk_id, order.voice_attachment)
    order.offer_outbox_id = vk.send_tracked_message(
        driver.vk_id,
        text,
        keyboard=kb.order_offer_keyboard(order.id, order.customer_price is not None),
        attachment=prepared_voice,
    )
    if not order.offer_outbox_id:
        log.error("Direct offer enqueue failed: order=%s driver=%s", order.id, driver.id)
        order.offered_driver_id = None
        return False
    set_state(session, driver.vk_id, States.D_OFFER, {"order_id": order.id})
    from . import push_service
    push_event = "customer_price" if order.customer_price is not None and not order.dispatcher_id else ("dispatcher_order" if order.dispatcher_id else "new_order")
    push_service.send(session, driver.id, push_event, order)
    timeout = (
        30
        if order.customer_price is not None and not order.dispatcher_id
        else get_int(session, "driver_accept_timeout", 60)
    )
    order_id = order.id
    driver_id = driver.id
    timers.schedule(
        "accept", order.id, timeout,
        lambda: _accept_timeout(order_id, driver_id),
    )
    return True


def offer_to_next_driver(
    session: Session,
    order: Order,
    temporary_exclude_driver_ids: set[int] | None = None,
    line_scope: str = "normal",
    line_name: str | None = None,
) -> bool:
    """Offer the order to the next waiting driver, or notify the creator that no
    drivers are available (all declined).
    """
    # Every path that can turn an unassigned order into an ordinary offer uses
    # the same row lock.  It serializes this transition with parallel take and
    # the route-offer timeout, preventing a last-writer-wins double assignment.
    locked_order = (
        session.query(Order)
        .filter(Order.id == order.id)
        .with_for_update()
        .one_or_none()
    )
    if (
        not locked_order
        or locked_order.driver_id is not None
        or locked_order.parallel_driver_id is not None
        or locked_order.status in (
            "assigned", "arrived", "in_progress", "parallel_assigned",
            "completed", "cancelled",
        )
        or (
            locked_order.status == "searching"
            and locked_order.offered_driver_id is not None
        )
    ):
        return False
    order = locked_order
    # The previous driver no longer holds this request: drop their notices.
    clear_offer_notice(session, order)
    declined = _declined_ids(session, order)
    excluded = set(declined) | set(temporary_exclude_driver_ids or ())
    from . import parallel_orders
    pickup_line = parallel_orders.free_line_city(order) or "Горнозаводск"
    route_city = parallel_orders.route_priority_city(order)
    if parallel_orders.local_village_line(order):
        line_name, line_scope = route_city, "exact"
    elif route_city and line_name is None and line_scope == "normal":
        # Village requests have a strict staged route.  Gornozavodsk is not
        # eligible until the automatic route-parallel stage is declined,
        # expires, or has no eligible recipient.
        line_name = (
            "Горнозаводск"
            if parallel_orders.is_route_fallback(order)
            else route_city
        )
        line_scope = "exact"
    driver = queue_service.next_waiting_driver(
        session,
        line_name or pickup_line,
        exclude_driver_ids=sorted(excluded),
        line_scope=line_scope,
    )
    creator = session.get(User, order.passenger_id)

    if driver is None:
        log.warning(
            "No waiting driver for order=%s pickup=%s declined=%s",
            order.id,
            order.pickup_city or order.line,
            declined,
        )
        _handle_no_driver(session, order)
        return False

    order.status = "searching"
    _set_current_offer(session, order, driver.id)
    # Reserve this driver immediately. Until they accept or decline, no other
    # ordinary order may be offered to the same driver.
    if not queue_service.mark_offered(session, driver):
        # A concurrent dispatch reserved this driver first. Clear our tentative
        # marker and continue with the next free driver.
        order.offered_driver_id = None
        order.status = "created"
        return offer_to_next_driver(
            session, order, temporary_exclude_driver_ids=excluded | {driver.id},
            line_scope=line_scope, line_name=line_name,
        )
    from . import passenger_queue
    passenger_queue.pause_actuality_prompts(session, except_order_id=order.id)
    log.info(
        "FIFO offer: order=%s driver=%s queue reservation=offered",
        order.id,
        driver.id,
    )

    text = f"🔔 Новая заявка #{order.number} ({order_type_label(order)})\nВаша заявка: {order_text(order)}"
    if order.customer_price is not None:
        text += f"\n💰 Цена клиента: {float(order.customer_price):.0f} ₽"
    if order.actuality_confirmed:
        text += "\n✅ Клиент подтвердил актуальность заявки."
    previous_reason = order.last_decline_reason
    # ``route_parallel_fallback`` is an internal routing stage, not a driver's
    # refusal.  Older rows may still carry it in last_decline_reason, but it
    # must never be shown as a previous driver's rejection.
    if previous_reason and previous_reason != parallel_orders.ROUTE_FALLBACK_REASON:
        reason_title = {"far": "дальние расстояния", "delivery": "доставка", "booking": "бронь", "away": "водитель отлучился", "need_address": "не хватает адреса", "spam": "спам", "dislike": "личная причина", "low_price": "маленькая стоимость"}.get(previous_reason, previous_reason)
        text += f"\nПричина отказа предыдущего водителя: {reason_title}"
    if creator and not _is_dispatcher_order(order):
        _full = creator.full_name or ("id" + str(creator.vk_id))
        text += "\n👤 От кого: [id%s|%s] %s" % (
            creator.vk_id, _full, passenger_rating_text(creator)
        )
    if order.comment:
        text += f"\n💬 Комментарий: {order.comment}"
    if order.dispatcher_id:
        if order.customer_name:
            text += f"\n👤 Пассажир: {order.customer_name}"
        if order.customer_phone:
            text += f"\n📞 Телефон: {order.customer_phone}"
        text += "\n🎧 Заявку создал диспетчер"
    text += _extras_summary(session, order)
    if order.night_surcharge:
        night_amount = get_int(session, "night_surcharge_amount", 50)
        text += f"\n🌙 Ночной тариф: +{night_amount} ₽ (23:00–06:00)"
    if is_delivery_type(order):
        text += "\nℹ️ При отказе от доставки ваше место в очереди сохраняется."
    prepared_voice = vk.prepare_voice_attachment(driver.vk_id, order.voice_attachment)
    # Track the exact offer card so its inline Поехать/Отказаться controls can
    # be removed immediately after the driver answers.
    order.offer_outbox_id = vk.send_tracked_message(
        driver.vk_id,
        text,
        keyboard=kb.order_offer_keyboard(order.id, order.customer_price is not None),
        attachment=prepared_voice,
    )
    # The passenger is notified only after the driver presses the accept
    # button, see notify_driver_found() called from the accept handler.
    # The order may previously have been advertised to busy drivers as a
    # parallel option. Tell those recipients that a free driver now has it.
    from . import parallel_orders
    parallel_orders.notify_assigned_to_free_driver(session, order, driver)
    set_state(session, driver.vk_id, States.D_OFFER, {"order_id": order.id})
    from . import push_service
    push_event = "customer_price" if order.customer_price is not None and not order.dispatcher_id else ("dispatcher_order" if order.dispatcher_id else "new_order")
    push_service.send(session, driver.id, push_event, order)

    # If the driver ignores the offer for the admin-configured interval, remove
    # them from the line immediately so they cannot delay following requests.
    timeout = (
        30
        if order.customer_price is not None and not order.dispatcher_id
        else get_int(session, "driver_accept_timeout", 60)
    )
    order_id = order.id
    driver_id = driver.id
    timers.schedule(
        "accept", order.id, timeout,
        lambda: _accept_timeout(order_id, driver_id),
    )
    return True


def _accept_timeout(order_id: int, driver_id: int) -> None:
    """Runs in a timer thread (requirement 8).

    An unanswered offer removes the driver from the line. If they explicitly
    return later, the same still-active request may be offered again because a
    timeout is not stored as a permanent refusal for that order.
    Every unanswered offer removes the driver from the line and returns them
    to the driver's main menu.
    Legacy wording marker: «Вы сняты с линии».
    """
    from common.database import session_scope

    with session_scope() as session:
        # Serialize timeout with accept/decline. Whichever transaction locks
        # the order first wins; the other one re-reads the final state.
        order = (
            session.query(Order)
            .filter(Order.id == order_id)
            .with_for_update()
            .one_or_none()
        )
        if order is None or order.status not in ("searching",):
            return
        current = _current_offer(session, order)
        if current != driver_id:
            return  # already moved on
        driver = session.get(User, driver_id)
        if driver:
            driver.driver_missed_offers = (driver.driver_missed_offers or 0) + 1
            reset(session, driver.vk_id, States.D_MENU)
            queue_service.leave_queue(session, driver)
            driver.is_on_line = False
            driver.driver_status = "offline"
            timeout_seconds = (
                30
                if order.customer_price is not None and not order.dispatcher_id
                else get_int(session, "driver_accept_timeout", 60)
            )
            timeout_text = (
                f"Вы не ответили на заявку за {timeout_seconds} секунд. "
                "Вас сняли с линии."
            )
            vk.send_message(
                driver.vk_id,
                timeout_text,
                # Legacy helper name retained in this branch for compatibility:
                # kb.missed_offer_timeout_keyboard(). The actual keyboard is
                # now the driver's main menu with on_line=False.
                keyboard=kb.driver_menu(on_line=False, show_role_switch=driver.has_role("dispatcher")),
            )
        finalize_offer_message(session, order, "⌛ Время ответа истекло.")
        # Clear both the durable order lock and the compatibility FSM marker.
        # SessionLocal has autoflush disabled, so flush the offline/queue/offer
        # state before selecting the next driver in this same transaction.
        clear_current_offer(session, order)
        session.flush()
        if order.requested_driver_id:
            order.status = "cancelled"
            order.cancelled_at = dt.datetime.now(dt.timezone.utc)
            order.cancelled_by = "system"
            passenger = session.get(User, order.passenger_id)
            if passenger:
                reset(session, passenger.vk_id, States.MAIN_MENU)
                from . import lines
                alternatives = [
                    candidate
                    for candidate in queue_service.free_drivers(session)
                    if candidate.id != driver_id
                    and candidate.is_on_line
                    and candidate.driver_status == "online"
                    and lines.has_complete_car(candidate)
                ]
                if alternatives:
                    passenger_text = (
                        "К сожалению, водитель не смог взять заявку, "
                        "можем предложить вам других водителей"
                    )
                    passenger_keyboard = kb.direct_driver_unavailable_keyboard()
                else:
                    passenger_text = (
                        "К сожалению, водитель не смог взять заявку, "
                        "дождитесь свободных водителей и повторите заявку."
                    )
                    passenger_keyboard = kb.passenger_menu()
                vk.send_message(
                    passenger.vk_id,
                    passenger_text,
                    keyboard=passenger_keyboard,
                )
            from . import passenger_queue
            passenger_queue.try_promote(session)
            return
        # Passenger-priced and dispatcher-created orders get one ordinary
        # offer first. Only after the driver ignores it do they become a
        # shared-chat request addressed to all drivers.
        from . import parallel_orders
        if (order.customer_price is not None or order.dispatcher_id) and not parallel_orders.local_village_line(order):
            order.status = "created"
            session.flush()
            publish_direct_to_requests_chat(session, order)
            from . import passenger_queue
            passenger_queue.try_promote(session)
            return
        # This exclusion applies only to this hand-off and is not persisted as
        # a refusal. A later pass may offer the same active request again.
        offer_to_next_driver(session, order, {driver_id})
        from . import passenger_queue
        passenger_queue.try_promote(session)


def _prearrival_notice(order_id: int) -> None:
    """Notify a passenger when the calculated remaining ETA reaches 2 min."""
    from common.database import session_scope
    from . import delivery_service

    with session_scope() as session:
        order = session.get(Order, order_id)
        if (
            order is None
            or order.status != "assigned"
            or order.dispatcher_id
            or delivery_service.is_delivery(order)
            or delivery_service.is_partner_delivery(order)
        ):
            return
        passenger = session.get(User, order.passenger_id) if order.passenger_id else None
        if passenger:
            vk.send_message(
                passenger.vk_id,
                "Водитель будет примерно через 3 минуты, можете собираться и выходить.",
            )


def schedule_prearrival_notice(session: Session, order: Order) -> None:
    """Schedule or move the reminder after an ETA extension."""
    timers.cancel("eta_prearrival", order.id)
    if not order.driver_departed_at or not order.arrival_eta or order.dispatcher_id:
        return
    # The three-minute reminder is useful only for an ETA initially longer
    # than seven minutes. For 7 minutes or less, do not schedule it at all.
    if int(order.arrival_eta) <= 7:
        return
    departed = order.driver_departed_at
    if departed.tzinfo is None:
        departed = departed.replace(tzinfo=dt.timezone.utc)
    deadline = departed + dt.timedelta(minutes=int(order.arrival_eta))
    delay = max(0.0, (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds() - 180.0)
    timers.schedule(
        "eta_prearrival",
        order.id,
        delay,
        lambda: _prearrival_notice(order.id),
    )



def clear_enroute_notice(session: Session, order: Order) -> None:
    """Delete only this order's obsolete passenger en-route notice."""
    notice_id = getattr(order, "enroute_notice_outbox_id", None)
    if notice_id:
        from . import outbox_service
        outbox_service.cancel_or_delete(session, notice_id)
        order.enroute_notice_outbox_id = None


def start_free_waiting(session: Session, order: Order) -> None:
    """Called when the driver marks 'arrived'. Starts the free-waiting timer."""
    timers.cancel("eta_prearrival", order.id)
    if getattr(order, "enroute_notice_outbox_id", None):
        from . import outbox_service
        outbox_service.cancel_or_delete(session, order.enroute_notice_outbox_id)
        order.enroute_notice_outbox_id = None
    if order.departure_prompt_outbox_id:
        from . import outbox_service
        # Preserve clickable driver name, rating, car and ETA when the passenger
        # did not answer. Only remove the now-obsolete Да/Нет keyboard.
        prompt = session.get(OutboxMessage, order.departure_prompt_outbox_id)
        if prompt and (prompt.text or "").strip() == "Ждете?":
            outbox_service.cancel_or_delete(session, order.departure_prompt_outbox_id)
        else:
            final_text = re.sub(
                r"\s*(?:(?:Вы )?[Жж]д[её]те(?: машинку)?\?)\s*$",
                "", (prompt.text or "") if prompt else "",
            ).rstrip()
            outbox_service.finalize_tracked_message(
                session,
                order.departure_prompt_outbox_id,
                final_text,
                replace_text=True,
                defer_edit=True,
            )
        order.departure_prompt_outbox_id = None
    order.status = "arrived"
    order.arrived_at = dt.datetime.now(dt.timezone.utc)
    # Waiting starts automatically on arrival and runs until «Пассажир сел».
    # The driver must not have to press a button before boarding.
    waiting_service.start_waiting(session, order)
    free_minutes = get_int(session, "free_waiting_minutes", 3)
    passenger = session.get(User, order.passenger_id)
    driver = session.get(User, order.driver_id) if order.driver_id else None
    if passenger and not _is_dispatcher_order(order):
        car = ", ".join(str(value) for value in (
            getattr(driver, "car_model", None), getattr(driver, "car_color", None),
            getattr(driver, "car_number", None),
        ) if value) or "не указана"
        vk.send_message(
            passenger.vk_id,
            f"🚘 Водитель на месте! Машина: {car}\n"
            f"⏰ У вас {free_minutes} мин бесплатного ожидания, далее {get_float(session, 'price_per_waiting_minute', 10):g} руб./мин.",
            keyboard=kb.passenger_arrived_keyboard(order.id),
        )
        set_state(session, passenger.vk_id, States.P_ARRIVED, {"order_id": order.id})
    elif _is_dispatcher_order(order) and order.dispatcher_id:
        dispatcher = session.get(User, order.dispatcher_id)
        if dispatcher:
            details = f"\n{dispatcher_driver_details(driver)}" if driver else ""
            vk.send_message(
                dispatcher.vk_id,
                f"🚘 Водитель приехал по заявке #{order.number}.{details}",
            )


def _extras_summary(session: Session, order: Order) -> str:
    """Textual list of passenger-selected services for drivers."""
    from . import extra_services

    parts = ""
    selection = extra_services.from_json(order.extra_services)
    lines = extra_services.describe(session, selection, with_prices=False)
    if lines:
        parts += "\n➕ Услуги: " + ", ".join(lines)
    return parts


def _free_waiting_expired(order_id: int) -> None:
    from common.database import session_scope

    with session_scope() as session:
        order = session.get(Order, order_id)
        if order is None or order.status != "arrived":
            return
        order.paid_waiting_started = True
        rate = _waiting_rate(session)
        passenger = session.get(User, order.passenger_id)
        driver = session.get(User, order.driver_id) if order.driver_id else None
        if passenger and not _is_dispatcher_order(order):
            vk.send_message(
                passenger.vk_id,
                f"⏱ Бесплатное ожидание закончилось. Далее {rate:.0f} ₽/мин.",
            )
        if driver:
            vk.send_message(driver.vk_id, "⏱ Началось платное ожидание.")


def compute_waiting_fee(session: Session, order: Order) -> float:
    """Compute the accumulated paid-waiting fee at the current moment."""
    if not order.arrived_at:
        return 0.0
    free_minutes = get_int(session, "free_waiting_minutes", 3)
    rate = _waiting_rate(session)
    now = dt.datetime.now(dt.timezone.utc)
    arrived = order.arrived_at
    if arrived.tzinfo is None:
        arrived = arrived.replace(tzinfo=dt.timezone.utc)
    waited_min = (now - arrived).total_seconds() / 60
    billable = max(waited_min - free_minutes, 0)
    return round(billable * rate, 2)


def _waiting_rate(session: Session) -> float:
    from common.settings_service import get_float

    return get_float(session, "price_per_waiting_minute", 10)


def _is_dispatcher_order(order: Order) -> bool:
    """True when the 'passenger' is really the dispatcher who created the order
    (no real VK passenger to notify with passenger keyboards).
    """
    return order.dispatcher_id is not None and order.dispatcher_id == order.passenger_id


# --- draft data helpers on the order ---------------------------------------- #
# Declined driver ids and the current offer are stashed on the creator's state
# data (keyed implicitly by the creator), avoiding extra columns.
from .states_service import set_state as _set_state  # noqa: E402


def _declined_ids(session: Session, order: Order) -> list[int]:
    try:
        return [int(value) for value in json.loads(order.declined_driver_ids or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _add_declined(session: Session, order: Order, driver_id: int) -> None:
    declined = set(_declined_ids(session, order))
    declined.add(driver_id)
    order.decline_count = len(declined)
    order.declined_driver_ids = json.dumps(sorted(declined))


def _set_current_offer(session: Session, order: Order, driver_id: int) -> None:
    order.offered_driver_id = driver_id
    creator = session.get(User, order.passenger_id)
    if creator:
        _set_state(session, creator.vk_id, data={"current_offer": driver_id})


def _current_offer(session: Session, order: Order) -> int | None:
    if order.offered_driver_id:
        return order.offered_driver_id
    creator = session.get(User, order.passenger_id)
    if not creator:
        return None
    return get_data(session, creator.vk_id).get("current_offer")


def clear_current_offer(session: Session, order: Order) -> None:
    """Release every ownership marker before handing an offer to another driver.

    ``offered_driver_id`` is the durable lock while ``current_offer`` is the
    passenger FSM compatibility marker. Leaving either one behind makes
    ``offer_to_next_driver`` treat a declined request as still reserved.
    """
    order.offered_driver_id = None
    creator = session.get(User, order.passenger_id)
    if creator:
        _set_state(session, creator.vk_id, data={"current_offer": None})


# --- Decline reasons + no-driver handling (requirements 4, 6) --------------- #
def add_decline(session: Session, order: Order, driver_id: int, reason_category: str | None = None) -> None:
    """Record a driver decline plus an optional reason category on the order."""
    _add_declined(session, order, driver_id)
    if reason_category:
        reasons = set(_declined_reasons(session, order))
        reasons.add(reason_category)
        order.decline_reasons_json = json.dumps(sorted(reasons), ensure_ascii=False)
        order.last_decline_reason = reason_category


def _declined_reasons(session: Session, order: Order) -> list[str]:
    try:
        return [str(value) for value in json.loads(order.decline_reasons_json or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _handle_no_driver(session: Session, order: Order) -> None:
    """No free driver could take the order: either report a specific reason
    (delivery / long-distance) or park the passenger in the waiting queue.
    """
    # A driver may become free or select a line between the initial lookup and
    # this fallback. Recheck before exposing the order as parallel: an eligible
    # free driver must always receive the ordinary offer first.
    if has_eligible_waiting_driver(session, order):
        log.info("Free driver appeared before parallel fallback: order=%s", order.id)
        creator = session.get(User, order.passenger_id)
        if creator and not order.dispatcher_id:
            vk.send_message(
                creator.vk_id,
                "⏰ Минутку…",
                keyboard=kb.passenger_waiting_keyboard(order.id),
            )
        offer_to_next_driver(session, order)
        return

    clear_offer_notice(session, order)
    creator = session.get(User, order.passenger_id)
    reasons = _declined_reasons(session, order)
    order.status = "no_drivers"

    if _is_dispatcher_order(order):
        from . import parallel_orders, passenger_queue

        passenger_queue.enqueue(session, order)
        route_offer_created = parallel_orders.notify_busy_drivers(session, order)
        if parallel_orders.route_priority_city(order) and not route_offer_created:
            parallel_orders._fallback_to_free_drivers(
                session, order, recheck_village=True
            )
        # Dispatcher orders stay in the queue silently. The dispatcher never
        # receives waiting confirmations or no-driver menus; only assignment
        # and arrival notifications are sent later.
        return

    # If all eligible drivers declined a long-distance or delivery request,
    # publish it to the separate requests chat. A driver can choose the «Доставка»
    # decline reason even for an order whose stored type is still regular, so
    # inspect both the order type and collected decline reasons.
    from . import parallel_orders
    fallback_to_chat = not parallel_orders.local_village_line(order) and (
        is_delivery_type(order)
        or "delivery" in reasons
        or "far" in reasons
    )
    if fallback_to_chat:
        raw_peer_id = get_int(session, "driver_fallback_chat_peer_id", 0)
        configured_peer_id = normalize_driver_chat_peer_id(raw_peer_id)
        if configured_peer_id and creator:
            order.status = "chat_search"
            reason_label = (
                "Доставка"
                if is_delivery_type(order) or "delivery" in reasons
                else "Дальнее расстояние"
            )
            order.chat_notice_outbox_id = send_fallback_chat_tracked_notice(
                session,
                f"🔔 Заявка №{order.number}\nПричина: {reason_label}\n"
                f"Маршрут: {order_text(order)}\n"
                + (
                    "🎧 Заявка от диспетчера"
                    if order.dispatcher_id
                    else f"Пассажир: [id{creator.vk_id}|{creator.full_name or ('id'+str(creator.vk_id))}]"
                ),
                keyboard=kb.chat_take_keyboard(order.id, order.number),
                attachment=order.voice_attachment,
            )
            if not order.chat_notice_outbox_id:
                order.status = "no_drivers"
                log.error(
                    "Could not publish order %s to fallback chat peer_id=%s raw_peer_id=%s",
                    order.id, configured_peer_id, raw_peer_id,
                )
                vk.send_message(
                    creator.vk_id,
                    "Не удалось передать заявку в чат заявок. Проверьте driver_fallback_chat_peer_id и доступ сообщества к беседе.",
                    keyboard=kb.passenger_waiting_keyboard(order.id),
                )
                return
            vk.send_message(
                creator.vk_id,
                "Заявка отправлена в чат заявок. Ожидаем, когда водитель возьмёт её.",
                keyboard=kb.passenger_waiting_keyboard(order.id),
            )
            timers.schedule(
                "driver_chat", order.id, CHAT_UNCLAIMED_TIMEOUT_SECONDS,
                lambda: _driver_chat_timeout(order.id),
            )
            return
        if creator and not configured_peer_id:
            log.error(
                "Fallback chat is not configured for order %s: driver_fallback_chat_peer_id=%s",
                order.id, raw_peer_id,
            )
            vk.send_message(
                creator.vk_id,
                "Чат заявок не настроен: укажите driver_fallback_chat_peer_id в настройках.",
                keyboard=kb.passenger_waiting_keyboard(order.id),
            )
            return
    if is_delivery_type(order) and not parallel_orders.local_village_line(order):
        if creator:
            vk.send_message(creator.vk_id, msg(session, "msg_delivery_no_drivers"), keyboard=kb.passenger_menu())
            reset(session, creator.vk_id, States.MAIN_MENU)
        return
    if "far" in reasons and not parallel_orders.local_village_line(order):
        if creator:
            vk.send_message(creator.vk_id, "\U0001F614 Нет водителей для дальних расстояний. Попробуйте позже.", keyboard=kb.passenger_menu())
            reset(session, creator.vk_id, States.MAIN_MENU)
        return

    # Otherwise everyone is simply busy → park in the waiting queue (requirement 4).
    from . import parallel_orders, passenger_queue

    passenger_queue.enqueue(session, order)
    # One route-compatible busy driver receives the durable 60-second offer.
    # If no such offer can be created, the caller immediately enters the
    # Gornozavodsk FIFO fallback without a second one-minute delay.
    route_city = parallel_orders.route_priority_city(order)
    route_offer_created = parallel_orders.notify_busy_drivers(session, order)
    if route_city and not route_offer_created:
        parallel_orders._fallback_to_free_drivers(
            session, order, recheck_village=True
        )
    if creator:
        vk.send_message(
            creator.vk_id,
            "⏰ Минутку…" if order.offered_driver_id or order.driver_id else
            "⏰ Чуть подождать, первый свободный, возьмет вашу заявку…",
            keyboard=kb.passenger_waiting_keyboard(order.id),
        )


def _driver_chat_timeout(order_id: int) -> None:
    from common.database import session_scope
    with session_scope() as session:
        order = session.get(Order, order_id)
        if not order or order.status != "chat_search":
            return
        delete_chat_order_notice(session, order)
        order.status = "cancelled"
        order.cancelled_at = dt.datetime.now(dt.timezone.utc)
        order.cancelled_by = "system"
        passenger = session.get(User, order.passenger_id)
        if passenger:
            label = driver_chat_reason_label(session, order) or "заявку"
            vk.send_message(passenger.vk_id, f"Не смогли найти водителя на {label}. Попробуйте заказать позже.", keyboard=kb.passenger_menu())
            reset(session, passenger.vk_id, States.MAIN_MENU)


def reassign_duplicate_pending_offers() -> int:
    """On startup, keep one pending card per driver and redistribute extras."""
    from collections import defaultdict
    from common.database import session_scope

    repaired = 0
    with session_scope() as session:
        pending = (
            session.query(Order)
            .filter(
                Order.status == "searching",
                Order.offered_driver_id.isnot(None),
            )
            .order_by(Order.offered_driver_id.asc(), Order.created_at.asc(), Order.id.asc())
            .all()
        )
        grouped: dict[int, list[Order]] = defaultdict(list)
        for order in pending:
            grouped[int(order.offered_driver_id)].append(order)
        for driver_id, orders in grouped.items():
            if len(orders) < 2:
                continue
            driver = session.get(User, driver_id)
            if driver:
                queue_service.mark_offered(session, driver)
            for duplicate in orders[1:]:
                timers.cancel("accept", duplicate.id)
                finalize_offer_message(
                    session, duplicate,
                    "Заявка передана следующему свободному водителю.",
                )
                duplicate.offered_driver_id = None
                duplicate.status = "created"
                offer_to_next_driver(
                    session, duplicate, temporary_exclude_driver_ids={driver_id}
                )
                repaired += 1
    return repaired
