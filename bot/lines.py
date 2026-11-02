"""«Линии» (города) — requirement 1.1.

А «линия» — это город, где работает водитель. Технически линии хранятся в таблице
``cities`` (так как очередь водителей уже привязана к city_id). Для удобства
водителю также проставляется users.current_line / users.is_on_line, а заказу —
 orders.line (название линии).

Модуль содержит сервисные функции и хендлеры выбора линии (водитель) и
вопроса после завершения поездки.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy.orm import Session

from common import audit
from common.logger import get_logger
from common.models import City, DriverQueue, User
from common.settings_service import get_setting, msg

from . import keyboards as kb, queue_service
from .roles import can_switch_role
from .states_service import States, reset, set_state
from .vk_client import vk

log = get_logger("bot.lines")

# Начальный набор линий (названия редактируются в админке).
DEFAULT_LINES = ["Кусья", "Пашия", "Горнозаводск", "Чусовой", "Сараны"]

# Common inflected spellings of the priority lines so an order that starts
# with e.g. «Пашии …» or «Кусьи …» is still routed to the Пашия / Кусья line.
# Keys are casefolded; values are the canonical line names.
LINE_ALIASES = {
    "пашия": "Пашия",
    "пашии": "Пашия",
    "кусья": "Кусья",
    "кусьи": "Кусья",
    "чусовой": "Чусовой",
    "чусового": "Чусовой",
    "чусовом": "Чусовой",
    "чусовому": "Чусовой",
    "чус": "Чусовой",
    "чуса": "Чусовой",
    "сараны": "Сараны",
    "саранов": "Сараны",
    "саранах": "Сараны",
}

ROUTE_LINE_PATTERNS = {
    "Пашия": r"паши(?:я|и|ю|е|ей)",
    "Кусья": r"кусь(?:я|и|ю|е|ей)",
    "Чусовой": r"(?:чус(?:а)?|чусов(?:ой|ого|ом|ому))",
    # Тёплая гора обслуживается водителями линии «Сараны».
    "Сараны": (
        r"(?:саран(?:ы|ов|ам|ах)|"
        r"т[её]пл(?:ая|ой|ую)\s+гор(?:а|ы|у|е))"
    ),
}


def split_route_segments(text: str | None) -> tuple[str | None, str | None]:
    """Split an explicit «откуда - куда» route without breaking house ranges."""
    normalized = " ".join((text or "").split())
    # ASCII '-' is a separator only with spaces; en/em dashes are unambiguous
    # and may be written without them. Thus «дом 5-7» remains one address.
    separator = re.search(r"\s+-\s+|[–—]", normalized)
    if not separator:
        return None, None
    origin = normalized[:separator.start()].strip()
    destination = normalized[separator.end():].strip()
    if not origin or not destination:
        return None, None
    return origin, destination


def _route_line_in(text: str | None) -> str | None:
    value = (text or "").casefold()
    for line_name, pattern in ROUTE_LINE_PATTERNS.items():
        if re.search(rf"\b{pattern}\b", value, re.IGNORECASE):
            return line_name
    return None


def _route_line_first_two(text: str | None) -> str | None:
    """Recognize a line name/phrase beginning at route word one or two."""
    value = (text or "").casefold()
    route_origin, _route_destination = split_route_segments(value)
    if route_origin:
        value = route_origin
    for line_name, pattern in ROUTE_LINE_PATTERNS.items():
        match = re.search(rf"\b{pattern}\b", value, re.IGNORECASE)
        if match and len(re.findall(r"[а-яё]+", value[:match.start()])) <= 1:
            return line_name
    return None


def route_origin_line(text: str | None) -> str | None:
    """Return the dispatch line named as the route's pickup point."""
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    origin_segment, _destination = split_route_segments(normalized)
    if origin_segment:
        line_name = _route_line_in(origin_segment)
        if line_name:
            return line_name
    value = normalized.casefold()
    for line_name, pattern in ROUTE_LINE_PATTERNS.items():
        if re.search(rf"\b(?:из|с)\s+{pattern}\b", value, re.IGNORECASE):
            return line_name
    divider = re.search(r"\bдо\b", value, re.IGNORECASE)
    if divider:
        line_name = _route_line_in(value[:divider.start()])
        if line_name:
            return line_name
    return _route_line_first_two(value)


def _resolve_alias(token, canonical):
    """Map an inflected token to a configured line name, if that line exists."""
    name = LINE_ALIASES.get(token)
    if name and name.casefold() in canonical:
        return canonical[name.casefold()]
    return None


def _clean_token(token: str) -> str:
    """Remove punctuation around a word without changing its inner spelling."""
    return token.strip(" \t\r\n.,;:!?()[]{}«»\"'–—-")


def parse_pickup_city(
    text: str,
    city_names: Iterable[str] | None = None,
) -> tuple[str | None, str]:
    """Return ``(pickup_city, remaining_route_text)``.

    The first two whitespace-separated words are checked first.  In addition,
    a configured city anywhere before the route separator «до» is the pickup:
    «от заимки Пашии до города» therefore starts in Pashiya, while
    «из города до Пашии» does not. Matching is case-insensitive and ignores
    punctuation around a token.

    ``city_names`` is optional to keep this helper easy to unit-test.  Runtime
    code passes the active city names from the admin-managed ``cities`` table.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None, ""

    configured = list(city_names or DEFAULT_LINES)
    canonical = {name.casefold(): name for name in configured if name and name.strip()}
    matches = list(re.finditer(r"\S+", normalized))
    for token_index in range(min(2, len(matches))):
        token = _clean_token(matches[token_index].group(0)).casefold()
        city = canonical.get(token) or _resolve_alias(token, canonical)
        if city:
            # Remove only the recognized city token.  Everything else remains
            # a single opaque route/destination string; it is not parsed again.
            start, end = matches[token_index].span()
            remaining = (normalized[:start] + normalized[end:]).strip()
            remaining = " ".join(remaining.split())
            return city, remaining

    # Natural requests often start with courtesy/quantity words, so Pashiya
    # or Kusya is no longer necessarily token one or two.  The first standalone
    # «до» is the direction separator; the city closest to it on the left is
    # the pickup point.  Never inspect the right side here, otherwise
    # «из города до Пашии» would be routed backwards.
    separator = re.search(r"\bдо\b", normalized, re.IGNORECASE)
    if separator:
        before = [match for match in matches if match.start() < separator.start()]
        for match in reversed(before):
            token = _clean_token(match.group(0)).casefold()
            city = canonical.get(token) or _resolve_alias(token, canonical)
            if city:
                start, end = match.span()
                remaining = (normalized[:start] + normalized[end:]).strip()
                return city, " ".join(remaining.split())

    # Multi-word aliases (Тёплая гора -> Сараны), explicit «из/с» forms and
    # the left side of an ASCII/en/em-dash route need phrase-aware parsing.
    route_line = route_origin_line(normalized)
    if route_line and route_line.casefold() in canonical:
        _origin, destination = split_route_segments(normalized)
        return canonical[route_line.casefold()], destination or normalized
    return None, normalized


def parse_pickup_city_for_session(session: Session, text: str) -> tuple[str | None, str]:
    """Parse using the active recognition cities configured in the admin UI."""
    return parse_pickup_city(text, (name for _, name in list_lines(session)))


def list_lines(session: Session) -> list[tuple[int, str]]:
    rows = (
        session.query(City)
        .filter(City.is_active.is_(True))
        .order_by(City.name)
        .all()
    )
    return [(c.id, c.name) for c in rows]


def line_name(session: Session, city_id: int | None) -> str | None:
    if not city_id:
        return None
    city = session.get(City, city_id)
    return city.name if city else None


def city_id_by_name(session: Session, name: str | None) -> int | None:
    if not name:
        return None
    city = session.query(City).filter(City.name == name).first()
    return city.id if city else None


def ensure_seed_lines(session: Session) -> None:
    """Idempotently add every built-in line without overwriting admin choices."""
    existing = {
        (city.name or "").strip().casefold()
        for city in session.query(City).all()
    }
    for name in DEFAULT_LINES:
        if name.casefold() not in existing:
            session.add(City(name=name, is_active=True))
    session.flush()


def line_has_free_drivers(session: Session, city_id: int | None) -> bool:
    """True if at least one driver is on this line and free (req 4)."""
    if not city_id:
        return False
    q = (
        session.query(DriverQueue)
        .join(User, User.id == DriverQueue.driver_id)
        .filter(
            DriverQueue.city_id == city_id,
            DriverQueue.status == "waiting",
            User.is_on_line.is_(True),
            User.driver_status == "online",
        )
    )
    return session.query(q.exists()).scalar() or False


def line_has_any_driver(session: Session, city_id: int | None) -> bool:
    """True if any driver is currently on this line (busy or free)."""
    if not city_id:
        return False
    q = session.query(User).filter(User.is_on_line.is_(True), User.current_line == line_name(session, city_id))
    return session.query(q.exists()).scalar() or False


# --------------------------------------------------------------------------- #
#  Requirement 3: automatic line detection for passenger orders               #
# --------------------------------------------------------------------------- #
def detect_line_from_text(session: Session, text: str | None) -> int | None:
    """Try to detect the line (city_id) from a free-text address.

    Matches any active line name occurring in the address text (case-insensitive),
    e.g. "Пашия, ул. Ленина" -> line «Пашия». Returns None if nothing matches.
    """
    if not text:
        return None
    low = text.lower()
    for cid, name in list_lines(session):
        if name and name.lower() in low:
            return cid
    return None


def default_line_city_id(session: Session) -> int | None:
    """Line assigned when it cannot be detected from the address (requirement 3).

    Uses the admin-configurable ``default_line`` setting, falling back to the
    first active line so an order is never left without a line.
    """
    name = get_setting(session, "default_line")
    cid = city_id_by_name(session, name) if name else None
    if cid:
        return cid
    lines = list_lines(session)
    return lines[0][0] if lines else None


def resolve_order_line(session: Session, *texts: str | None) -> tuple[int | None, str | None]:
    """Return (city_id, line_name) for a passenger order: detect from any of the
    supplied address texts, else use the default line (requirement 3).
    """
    for text in texts:
        cid = detect_line_from_text(session, text)
        if cid:
            return cid, line_name(session, cid)
    cid = default_line_city_id(session)
    return cid, line_name(session, cid)


def free_drivers_overview(session: Session) -> str:
    """Per-line free drivers: full names only, comma-separated."""
    header = msg(session, "msg_lines_overview_header")
    none_label = get_setting(session, "msg_line_no_free") or "нет свободных"
    parts = [header]
    for cid, name in list_lines(session):
        free = queue_service.free_drivers_on_line(session, cid)
        if free:
            people = ", ".join((d.full_name or "Водитель").strip() for d in free)
            parts.append(f"• {name}: {people}")
        else:
            parts.append(f"• {name}: {none_label}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
#  Driver handlers                                                             #
# --------------------------------------------------------------------------- #
def has_complete_car(driver: User) -> bool:
    """A driver may join a line only after all required car fields are saved."""
    return bool(
        (driver.car_model or "").strip()
        and (driver.car_color or "").strip()
        and (driver.car_number or "").strip()
    )


def require_complete_car(session: Session, driver: User) -> bool:
    """Keep an incomplete driver out of every normal line-entry path."""
    if has_complete_car(driver):
        return True
    vk.send_message(
        driver.vk_id,
        "🚗 Сначала полностью заполните «Моя машина»: марка, цвет и госномер.",
        keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(driver)),
    )
    return False


def show_line_menu(session: Session, driver: User) -> None:
    lines = list_lines(session)
    if not lines:
        vk.send_message(driver.vk_id, "Линии пока не настроены. Обратитесь к администратору.")
        return
    set_state(session, driver.vk_id, States.D_SELECT_LINE)
    # Requirement 2: show which drivers are free on each line above the picker.
    overview = free_drivers_overview(session)
    vk.send_message(
        driver.vk_id,
        f"{overview}\n\n{msg(session, 'msg_choose_line')}",
        keyboard=kb.lines_keyboard(lines, cmd="set_line"),
    )


def set_driver_line(session: Session, driver: User, city_id: int) -> None:
    # The line picker itself is an entry path, so it must not bypass car validation.
    if not require_complete_car(session, driver):
        return
    name = line_name(session, city_id)
    if not name:
        vk.send_message(driver.vk_id, "Линия не найдена. Выберите из списка.")
        return show_line_menu(session, driver)
    driver.current_line = name
    driver.is_on_line = True
    driver.driver_status = "online"
    queue_service.join_queue(session, driver, city_id)
    reset(session, driver.vk_id, States.D_MENU)
    vk.send_message(
        driver.vk_id,
        msg(session, "msg_line_selected", line=name),
        keyboard=kb.driver_menu(on_line=True, show_role_switch=can_switch_role(driver)),
    )
    audit.record(session, "driver_line_selected", f"driver={driver.id} line={name}")
    from . import passenger_queue
    passenger_queue.try_promote(session)


def leave_line(session: Session, driver: User) -> None:
    queue_service.leave_queue(session, driver)
    driver.is_on_line = False
    driver.driver_status = "offline"
    reset(session, driver.vk_id, States.D_MENU)
    vk.send_message(
        driver.vk_id,
        msg(session, "msg_line_left"),
        keyboard=kb.driver_menu(on_line=False, show_role_switch=can_switch_role(driver)),
    )
    audit.record(session, "driver_line_left", f"driver={driver.id}")


def ask_post_ride_line(session: Session, driver: User) -> None:
    """Req 1.1: after finishing a ride, ask stay / change / leave."""
    # The driver must make an explicit line choice before receiving another
    # order. Keep the queue row unavailable while this menu is open.
    queue_service.set_away(session, driver)
    name = driver.current_line
    if not name:
        return show_line_menu(session, driver)
    set_state(session, driver.vk_id, States.D_POST_RIDE_LINE)
    vk.send_message(
        driver.vk_id,
        msg(session, "msg_post_ride_line", line=name),
        keyboard=kb.post_ride_line_keyboard(name),
    )


def stay_line(session: Session, driver: User) -> None:
    # Returning after a trip is also a new queue entry and needs the same gate.
    if not require_complete_car(session, driver):
        return
    name = driver.current_line
    if not name:
        return show_line_menu(session, driver)
    driver.is_on_line = True
    driver.driver_status = "online"
    cid = city_id_by_name(session, name)
    if cid:
        queue_service.join_queue(session, driver, cid)
    reset(session, driver.vk_id, States.D_MENU)
    vk.send_message(
        driver.vk_id,
        msg(session, "msg_line_stay", line=name),
        keyboard=kb.driver_menu(on_line=True, show_role_switch=can_switch_role(driver)),
    )
    audit.record(session, "driver_line_stay", f"driver={driver.id} line={name}")
    from . import passenger_queue
    passenger_queue.try_promote(session)
