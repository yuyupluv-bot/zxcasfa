"""Customer-specified fares and atomic driver counter-offers."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from common import audit, time_utils
from common.models import Order, User
from common.settings_service import get_int

from . import keyboards as kb, order_service, passenger_queue, queue_service, timers
from .states_service import States, reset, set_state
from .vk_client import vk

MIN_PRICE = 150
MAX_PRICE = 50_000
COUNTER_OFFER_TIMEOUT_SECONDS = 90

_NUMBER = r"(?:\d{1,2}(?:[\s\u00a0]\d{3})+|\d{3,5})"
# Currency-tagged amounts are recognized anywhere. Besides normal Russian
# spellings support Latin R and the literal VVV requested for imported text.
_CURRENCY = r"(?:₽|[рr]\.?|руб\.?|рубль|рубля|рублей|vvv)"
# For a currency-tagged fare use the last numeric token. This avoids treating
# an address such as «Свободы 5 170 рублей» as 5170: the 5 is the house number
# and 170 is the fare. Newlines and ordinary spaces around the fare are valid.
_TAGGED_RE = re.compile(rf"(?<!\d)(?P<amount>\d{{3,5}})\s*{_CURRENCY}(?![а-яёa-z])", re.I)
# In free-form requests, a bare price must be one contiguous 3–5 digit token.
# This prevents an address number plus a fare (for example «Ленина 12 150»)
# from being merged into the false amount 12 150.
_BARE_END_RE = re.compile(r"(?<!\d)(?P<amount>\d{3,5})\s*[.,;:!?-]*\s*$", re.I)


def _amount(raw: str) -> int | None:
    try:
        value = int(re.sub(r"[\s\u00a0]", "", raw))
    except (TypeError, ValueError):
        return None
    return value if MIN_PRICE <= value <= MAX_PRICE else None


def parse_entered_price(text: str | None) -> int | None:
    """Parse a standalone fare without applying the minimum/maximum limits."""
    normalized = " ".join((text or "").split())
    match = re.fullmatch(rf"(?P<amount>{_NUMBER})\s*{_CURRENCY}?", normalized, re.I)
    if not match:
        return None
    try:
        return int(re.sub(r"[\s\u00a0]", "", match.group("amount")))
    except (TypeError, ValueError):
        return None


def extract_customer_price(text: str | None) -> tuple[str, int | None]:
    """Extract a fare only when its number is from 150 through 50,000.

    Smaller numbers are never treated as an invalid fare: they remain intact
    in the request because they can be a house, apartment or entrance number.
    """
    source = " ".join((text or "").split())
    match = next((m for m in _TAGGED_RE.finditer(source) if _amount(m.group("amount")) is not None), None)
    if match is None:
        candidate = _BARE_END_RE.search(source)
        match = candidate if candidate and _amount(candidate.group("amount")) is not None else None
    if match is None:
        return source, None
    value = _amount(match.group("amount"))
    cleaned = (source[:match.start()] + " " + source[match.end():]).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"^[,.;:—–-]+\s*|\s*[,.;:—–-]+$", "", cleaned).strip()
    return cleaned or source, value


def parse_driver_price(text: str | None) -> int | None:
    value = parse_entered_price(text)
    return value if value is not None and MIN_PRICE <= value <= MAX_PRICE else None


def has_price(order: Order) -> bool:
    return order.customer_price is not None


def price_line(order: Order) -> str:
    return f"\n💰 Цена клиента: {float(order.customer_price):.0f} ₽" if has_price(order) else ""


def start_passenger_price_edit(session: Session, passenger: User, order_id: int) -> None:
    order = (
        session.query(Order)
        .filter(
            Order.id == int(order_id or 0),
            Order.passenger_id == passenger.id,
            Order.status == "chat_search",
            Order.customer_price.isnot(None),
            Order.driver_id.is_(None),
        )
        .one_or_none()
    )
    if not order:
        return vk.send_message(passenger.vk_id, "Цена этой заявки уже недоступна для изменения.")
    set_state(
        session,
        passenger.vk_id,
        States.P_CUSTOM_PRICE_EDIT,
        {"order_id": order.id},
        merge=False,
    )
    vk.send_message(
        passenger.vk_id,
        f"Введите новую цену для заявки #{order.number} от {MIN_PRICE} до "
        f"{MAX_PRICE:,} рублей.".replace(",", " "),
        keyboard=kb.customer_price_waiting_keyboard(order.id),
    )


def wait_for_first_driver(session: Session, passenger: User, order_id: int) -> None:
    order = (
        session.query(Order)
        .filter(
            Order.id == int(order_id or 0),
            Order.passenger_id == passenger.id,
            Order.status == "chat_search",
            Order.customer_price.isnot(None),
        )
        .one_or_none()
    )
    if not order:
        return vk.send_message(passenger.vk_id, "Заявка уже недоступна.")
    set_state(
        session, passenger.vk_id, States.P_WAITING,
        {"order_ids": [order.id]}, merge=False,
    )
    vk.send_message(
        passenger.vk_id,
        "Продолжаем ждать первого водителя, который согласится на вашу цену.",
        keyboard=kb.customer_price_waiting_keyboard(order.id),
    )


def submit_passenger_price_edit(session: Session, passenger: User, text: str) -> None:
    from .states_service import get_data

    order_id = get_data(session, passenger.vk_id).get("order_id")
    order = (
        session.query(Order)
        .filter(Order.id == int(order_id or 0))
        .with_for_update()
        .one_or_none()
    )
    if (
        not order or order.passenger_id != passenger.id
        or order.status != "chat_search" or not has_price(order)
        or order.driver_id is not None
    ):
        reset(session, passenger.vk_id, States.MAIN_MENU)
        return vk.send_message(passenger.vk_id, "Цена этой заявки уже недоступна для изменения.")

    amount = parse_driver_price(text)
    if amount is None:
        return vk.send_message(
            passenger.vk_id,
            "Введите сумму от 150 до 50 000 рублей, например 170.",
            keyboard=kb.customer_price_waiting_keyboard(order.id),
        )
    old_amount = int(order.customer_price)
    if amount == old_amount:
        return vk.send_message(
            passenger.vk_id,
            "Новая цена совпадает с текущей. Введите другую сумму.",
            keyboard=kb.customer_price_waiting_keyboard(order.id),
        )
    if not order_service.replace_customer_price_chat_notice(
        session, order, old_amount, amount
    ):
        return vk.send_message(
            passenger.vk_id,
            "Не удалось обновить сообщение в водительском чате. Попробуйте ещё раз.",
            keyboard=kb.customer_price_waiting_keyboard(order.id),
        )

    order.customer_price = amount
    set_state(
        session, passenger.vk_id, States.P_WAITING,
        {"order_ids": [order.id]}, merge=False,
    )
    audit.record(
        session, "customer_price_changed",
        f"order={order.id} old_price={old_amount} new_price={amount}",
    )
    vk.send_message(
        passenger.vk_id,
        f"Цена заявки #{order.number} изменена: {old_amount} руб. → {amount} руб.",
        keyboard=kb.customer_price_waiting_keyboard(order.id),
    )


def start_from_private_offer(session: Session, driver: User, order_id: int) -> None:
    order = session.query(Order).filter(Order.id == int(order_id or 0)).with_for_update().one_or_none()
    if (
        not order or not has_price(order) or order.status != "searching"
        or order.offered_driver_id != driver.id or order.driver_id is not None
    ):
        return vk.send_message(driver.vk_id, "Заявка или предложение цены уже недоступны.")
    # The driver made an explicit decision to negotiate. Stop the ordinary
    # accept timeout while the amount is being typed; the queue row remains
    # reserved as ``offered`` so no other request can reach this driver.
    timers.cancel("accept", order.id)
    set_state(session, driver.vk_id, States.D_CUSTOM_PRICE, {"order_id": order.id}, merge=False)
    vk.send_message(
        driver.vk_id,
        f"Какую цену вы хотите предложить клиенту по заявке #{order.number}? "
        f"Введите сумму от {MIN_PRICE} до {MAX_PRICE:,} рублей.".replace(",", " "),
    )


def reserve_from_chat(session: Session, driver: User, order_id: int) -> None:
    order = session.query(Order).filter(Order.id == int(order_id or 0)).with_for_update().one_or_none()
    if not order or not has_price(order) or order.status != "chat_search" or order.driver_id:
        return vk.send_message(driver.vk_id, "Заявка уже недоступна.")
    from .handlers import active_order_for
    if active_order_for(session, driver, as_driver=True):
        return vk.send_message(driver.vk_id, "Сначала завершите текущую поездку.")
    timers.cancel("driver_chat", order.id)
    passenger_queue.remove(session, order.id)
    order_service.finalize_chat_order_notice(
        session, order, driver, considering_price=True
    )
    order.driver_id = driver.id
    order.price_offer_driver_id = driver.id
    order.status = "price_negotiation"
    queue_service.mark_considering(session, driver)
    set_state(session, driver.vk_id, States.D_CUSTOM_PRICE, {"order_id": order.id}, merge=False)
    session.flush()
    vk.send_message(
        driver.vk_id,
        f"Заявка #{order.number} закреплена за вами. Какую цену вы хотите предложить клиенту? "
        f"Введите сумму от {MIN_PRICE} до {MAX_PRICE:,} рублей.".replace(",", " "),
    )
    audit.record(session, "customer_price_chat_reserved", f"order={order.id} driver={driver.id}")


def submit(session: Session, driver: User, text: str) -> None:
    from .states_service import get_data
    order_id = get_data(session, driver.vk_id).get("order_id")
    order = session.query(Order).filter(Order.id == int(order_id or 0)).with_for_update().one_or_none()
    if not order or not has_price(order):
        reset(session, driver.vk_id, States.D_MENU)
        return vk.send_message(driver.vk_id, "Заявка уже недоступна.")
    owns_private = order.status == "searching" and order.offered_driver_id == driver.id
    owns_chat = order.status == "price_negotiation" and order.driver_id == driver.id
    if not (owns_private or owns_chat):
        reset(session, driver.vk_id, States.D_MENU)
        return vk.send_message(driver.vk_id, "Заявка уже недоступна.")
    amount = parse_driver_price(text)
    if amount is None:
        return vk.send_message(driver.vk_id, "Введите сумму от 150 до 50 000 рублей, например 250.")
    timers.cancel("accept", order.id)
    if owns_private:
        order_service.finalize_offer_message(session, order, f"💬 Вы предложили клиенту {amount} ₽.")
        order.driver_id = driver.id
        order.price_offer_driver_id = driver.id
        order.status = "price_negotiation"
        queue_service.mark_considering(session, driver)
    order.driver_proposed_price = amount
    passenger = session.get(User, order.passenger_id)
    if not passenger or order.dispatcher_id:
        return _accept(session, order, passenger, driver)
    order.price_offer_outbox_id = vk.send_tracked_message(
        passenger.vk_id,
        f"Водитель согласен вас отвезти за {amount} рублей.",
        keyboard=kb.customer_price_confirm_keyboard(order.id),
    )
    set_state(session, passenger.vk_id, States.P_CUSTOM_PRICE_CONFIRM, {"order_id": order.id}, merge=False)
    set_state(session, driver.vk_id, States.D_OFFER, {"order_id": order.id}, merge=False)
    vk.send_message(driver.vk_id, "Предложение отправлено клиенту. Ожидаем ответ 90 секунд.")
    timers.schedule("customer_price", order.id, COUNTER_OFFER_TIMEOUT_SECONDS, lambda: timeout(order.id))
    audit.record(session, "customer_price_offered", f"order={order.id} driver={driver.id} price={amount}")


def _finish_prompt(session: Session, order: Order, text: str) -> None:
    if not order.price_offer_outbox_id:
        return
    from . import outbox_service
    outbox_service.finalize_tracked_message(session, order.price_offer_outbox_id, text)
    order.price_offer_outbox_id = None


def passenger_response(session: Session, passenger: User, order_id: int, agree: bool) -> None:
    order = session.query(Order).filter(Order.id == int(order_id or 0)).with_for_update().one_or_none()
    if (
        not order or order.passenger_id != passenger.id or order.status != "price_negotiation"
        or not order.price_offer_driver_id or order.driver_proposed_price is None
    ):
        return vk.send_message(passenger.vk_id, "Предложение цены уже недоступно.")
    timers.cancel("customer_price", order.id)
    _finish_prompt(session, order, "✅ Вы согласились с ценой." if agree else "❌ Вы отказались от цены.")
    driver = session.get(User, order.price_offer_driver_id)
    if agree and driver:
        return _accept(session, order, passenger, driver)
    _cancel(session, order, passenger, driver, "Клиент отказался от предложенной цены. Заявка отменена.")


def _accept(session: Session, order: Order, passenger: User | None, driver: User) -> None:
    from . import handlers
    order.agreed_price = order.driver_proposed_price or order.customer_price
    order.offered_driver_id = driver.id
    order.status = "searching"
    order.driver_id = None
    order.price_offer_driver_id = None
    order.driver_proposed_price = None
    if passenger:
        vk.send_message(
            passenger.vk_id,
            f"✅ Вы согласились на поездку за {float(order.agreed_price):.0f} рублей. "
            "Водитель выбирает время подачи автомобиля.",
        )
    handlers.driver_accept(
        session,
        driver,
        order.id,
        notify_passenger=False,
        reserved_price_offer=True,
    )


def _cancel(session: Session, order: Order, passenger: User | None, driver: User | None, driver_text: str) -> None:
    order.status = "cancelled"
    order.cancelled_at = time_utils.now()
    order.cancelled_by = "passenger"
    order.offered_driver_id = None
    order.driver_id = None
    order.price_offer_driver_id = None
    order.driver_proposed_price = None
    passenger_queue.remove(session, order.id)
    if driver:
        queue_service.restore_position(session, driver)
        reset(session, driver.vk_id, States.D_MENU)
        vk.send_message(driver.vk_id, driver_text, keyboard=kb.driver_menu(True, False))
    if passenger:
        reset(session, passenger.vk_id, States.MAIN_MENU)
        vk.send_message(passenger.vk_id, "Заявка полностью отменена.", keyboard=kb.passenger_menu())
    audit.record(session, "customer_price_cancelled", f"order={order.id}")


def timeout(order_id: int) -> None:
    from common.database import session_scope
    with session_scope() as session:
        order = session.query(Order).filter(Order.id == int(order_id)).with_for_update().one_or_none()
        if not order or order.status != "price_negotiation":
            return
        passenger = session.get(User, order.passenger_id)
        driver = session.get(User, order.price_offer_driver_id) if order.price_offer_driver_id else None
        _finish_prompt(session, order, "⌛ Время ответа истекло. Заявка отменена.")
        _cancel(session, order, passenger, driver, "Клиент не ответил за 90 секунд. Заявка отменена.")
