"""Reference hourly schedule for drivers.

The schedule is deliberately informational only: it does not join the queue,
reserve a driver, or affect order dispatch.  Only today and tomorrow are
editable, which keeps stale marks from turning into hidden operational state.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from sqlalchemy.orm import Session

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import DriverScheduleMark, User, ROLE_DRIVER
from common.settings_service import get_setting, set_setting

from . import keyboards as kb
from .states_service import States, set_state
from .vk_client import vk

log = get_logger("bot.driver_schedule")

SLOT_COUNT = 24
SLOTS_PER_PAGE = 8
PAGE_COUNT = (SLOT_COUNT + SLOTS_PER_PAGE - 1) // SLOTS_PER_PAGE
REMINDER_POLL_SECONDS = 20
_started = False
_start_lock = threading.Lock()


def _today() -> dt.date:
    return time_utils.now().date()


def _date_for(day: str | None) -> dt.date:
    return _today() + dt.timedelta(days=1 if day == "tomorrow" else 0)


def _day_token(value: dt.date) -> str:
    return "tomorrow" if value == _today() + dt.timedelta(days=1) else "today"


def _day_title(value: dt.date) -> str:
    token = _day_token(value)
    label = "сегодня" if token == "today" else "завтра"
    return f"{label} ({value.strftime('%d.%m')})"


def _hour_label(hour: int) -> str:
    return f"{hour:02d}:00–{(hour + 1) % 24:02d}:00"


def _display_name(driver_id: int, full_name: str | None) -> str:
    value = " ".join(str(full_name or "").split()).replace("|", " ").replace("]", " ")
    return value[:100] or f"id{driver_id}"


def schedule_text(session: Session, schedule_date: dt.date) -> str:
    """Build the shared «who marked which hour» view."""
    rows = (
        session.query(
            DriverScheduleMark.hour,
            User.id,
            User.full_name,
        )
        .join(User, User.id == DriverScheduleMark.driver_id)
        .filter(DriverScheduleMark.schedule_date == schedule_date)
        .order_by(DriverScheduleMark.hour.asc(), User.full_name.asc(), User.id.asc())
        .all()
    )
    by_hour: dict[int, list[str]] = {}
    for hour, driver_id, full_name in rows:
        by_hour.setdefault(int(hour), []).append(
            _display_name(int(driver_id), full_name)
        )
    lines = [f"🗓 График на {_day_title(schedule_date)}:", ""]
    if not by_hour:
        lines.append("Пока никто не отметил часы.")
    else:
        for hour in range(SLOT_COUNT):
            names = by_hour.get(hour)
            if names:
                lines.append(f"• {_hour_label(hour)} — {', '.join(names)}")
    return "\n".join(lines)


def marks_for(
    session: Session,
    driver: User,
    schedule_date: dt.date,
) -> set[int]:
    return {
        int(hour)
        for (hour,) in session.query(DriverScheduleMark.hour)
        .filter(
            DriverScheduleMark.driver_id == driver.id,
            DriverScheduleMark.schedule_date == schedule_date,
        )
        .all()
    }


def _valid_date(schedule_date: dt.date) -> bool:
    return schedule_date in (_today(), _today() + dt.timedelta(days=1))


def show_menu(session: Session, driver: User) -> None:
    """Open the current shared schedule immediately."""
    show_day(session, driver, "today")


def show_day(session: Session, driver: User, day: str | None) -> None:
    schedule_date = _date_for(day)
    marks = marks_for(session, driver, schedule_date)
    token = _day_token(schedule_date)
    vk.send_message(
        driver.vk_id,
        schedule_text(session, schedule_date),
        keyboard=kb.driver_schedule_day(token, bool(marks)),
    )
    set_state(session, driver.vk_id, States.D_MENU)


def send_chat_schedule(
    session: Session,
    peer_id: int,
    day: str | None = None,
) -> None:
    """Reply to !график commands in the common driver conversation."""
    schedule_date = _date_for(day)
    vk.send_message(peer_id, schedule_text(session, schedule_date))


def edit_day(
    session: Session,
    driver: User,
    day: str | None,
    page: int = 1,
) -> None:
    schedule_date = _date_for(day)
    token = _day_token(schedule_date)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(PAGE_COUNT, page))
    marks = marks_for(session, driver, schedule_date)
    selected = len(marks)
    vk.send_message(
        driver.vk_id,
        f"✏️ Выберите часы на {_day_title(schedule_date)}.\n"
        f"Отмечено: {selected} из {SLOT_COUNT}.",
        keyboard=kb.driver_schedule_slots(token, marks, page),
    )
    set_state(session, driver.vk_id, States.D_MENU)


def toggle(
    session: Session,
    driver: User,
    day: str | None,
    hour: int | str | None,
    page: int = 1,
) -> None:
    schedule_date = _date_for(day)
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        return edit_day(session, driver, day, page)
    if not _valid_date(schedule_date) or not 0 <= hour < SLOT_COUNT:
        return show_menu(session, driver)

    mark = (
        session.query(DriverScheduleMark)
        .filter(
            DriverScheduleMark.driver_id == driver.id,
            DriverScheduleMark.schedule_date == schedule_date,
            DriverScheduleMark.hour == hour,
        )
        .one_or_none()
    )
    if mark is None:
        session.add(DriverScheduleMark(
            driver_id=driver.id,
            schedule_date=schedule_date,
            hour=hour,
        ))
    else:
        session.delete(mark)
    session.flush()
    edit_day(session, driver, day, page)


def _reminder_drivers(session: Session, only_without_marks: bool) -> list[User]:
    tomorrow = _today() + dt.timedelta(days=1)
    drivers = (
        session.query(User)
        .filter(User.is_blocked.is_(False))
        .order_by(User.id.asc())
        .all()
    )
    result = []
    for driver in drivers:
        if not driver.has_role(ROLE_DRIVER):
            continue
        if only_without_marks and marks_for(session, driver, tomorrow):
            continue
        result.append(driver)
    return result


def _send_reminder_once(session: Session, hour: int, only_without_marks: bool) -> int:
    now = time_utils.now()
    today = now.date()
    setting_key = f"driver_schedule_reminder_{hour}_date"
    if now.hour != hour:
        return 0
    if get_setting(session, setting_key, "") == today.isoformat():
        return 0
    text = (
        "🗓 Напоминание: отметьтесь в графике на завтра."
        if not only_without_marks
        else "⏰ Вы ещё не отметились в графике на завтра."
    )
    sent = 0
    for driver in _reminder_drivers(session, only_without_marks):
        if vk.send_message(
            driver.vk_id,
            text,
            keyboard=kb.driver_schedule_reminder_keyboard(),
        ):
            sent += 1
    set_setting(session, setting_key, today.isoformat())
    return sent


def _worker() -> None:
    while True:
        try:
            with session_scope() as session:
                first = _send_reminder_once(session, 18, False)
                second = _send_reminder_once(session, 20, True)
                if first or second:
                    log.info(
                        "Driver schedule reminders sent: 18=%s, 20=%s",
                        first,
                        second,
                    )
        except Exception:
            log.exception("Driver schedule reminder failed")
        time.sleep(REMINDER_POLL_SECONDS)


def start_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        threading.Thread(
            target=_worker,
            name="driver-schedule-reminders",
            daemon=True,
        ).start()
        _started = True
        log.info("Driver schedule reminder worker started")