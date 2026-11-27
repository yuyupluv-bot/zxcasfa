"""Reference hourly schedule for drivers.

The schedule is deliberately informational only: it does not join the queue,
reserve a driver, or affect order dispatch.  Only today and tomorrow are
editable, which keeps stale marks from turning into hidden operational state.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from common import time_utils
from common.models import DriverScheduleMark, User

from . import keyboards as kb
from .states_service import States, set_state
from .vk_client import vk

SLOT_COUNT = 24
SLOTS_PER_PAGE = 8
PAGE_COUNT = (SLOT_COUNT + SLOTS_PER_PAGE - 1) // SLOTS_PER_PAGE


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
    """Show the two available days without changing any schedule marks."""
    tomorrow = _today() + dt.timedelta(days=1)
    tomorrow_has_marks = bool(marks_for(session, driver, tomorrow))
    vk.send_message(
        driver.vk_id,
        "🗓 График водителя\n\n"
        "График носит справочный характер и не влияет на очередь и заявки.",
        keyboard=kb.driver_schedule_menu(tomorrow_has_marks),
    )
    set_state(session, driver.vk_id, States.D_MENU)


def show_day(session: Session, driver: User, day: str | None) -> None:
    schedule_date = _date_for(day)
    marks = marks_for(session, driver, schedule_date)
    lines = [
        f"🗓 График на {_day_title(schedule_date)}:",
        "",
    ]
    for hour in range(SLOT_COUNT):
        state = "✅ отмечено" if hour in marks else "▫️ свободно"
        lines.append(f"{state}  {_hour_label(hour)}")
    lines.extend((
        "",
        "Отметки нужны только для справки.",
    ))
    token = _day_token(schedule_date)
    vk.send_message(
        driver.vk_id,
        "\n".join(lines),
        keyboard=kb.driver_schedule_day(token, bool(marks)),
    )
    set_state(session, driver.vk_id, States.D_MENU)


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