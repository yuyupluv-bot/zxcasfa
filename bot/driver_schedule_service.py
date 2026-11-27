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
from common.models import (
    DriverScheduleDispatcher,
    DriverScheduleMark,
    DriverScheduleOff,
    ROLE_DISPATCHER,
    ROLE_DRIVER,
    User,
)
from common.settings_service import get_setting, set_setting

from . import keyboards as kb
from .roles import can_switch_role
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


def _mention(user_id: int, full_name: str | None) -> str:
    return f"[id{user_id}|{_display_name(user_id, full_name)}]"


def _week_start(schedule_date: dt.date) -> dt.date:
    return schedule_date - dt.timedelta(days=schedule_date.weekday())


def _dispatcher_assignment_week(schedule_date: dt.date) -> dt.date:
    """On Sunday a click assigns the dispatcher for the coming Monday week."""
    if schedule_date.weekday() == 6:
        return schedule_date + dt.timedelta(days=1)
    return _week_start(schedule_date)


def _dispatcher_for_date(session: Session, schedule_date: dt.date):
    return (
        session.query(DriverScheduleDispatcher, User)
        .join(User, User.id == DriverScheduleDispatcher.dispatcher_id)
        .filter(DriverScheduleDispatcher.week_start == _week_start(schedule_date))
        .one_or_none()
    )


def _plural_drivers(count: int) -> str:
    count = int(count)
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        return "водитель"
    if last in (2, 3, 4) and last_two not in (12, 13, 14):
        return "водителя"
    return "водителей"


def _ranges(values: list[tuple[int, object]]) -> list[tuple[int, int, object]]:
    """Merge adjacent hours while the displayed value stays identical."""
    result: list[tuple[int, int, object]] = []
    for hour, value in values:
        if result and result[-1][1] + 1 == hour and result[-1][2] == value:
            start, _end, old_value = result[-1]
            result[-1] = (start, hour, old_value)
        else:
            result.append((hour, hour, value))
    return result


def schedule_text(
    session: Session,
    schedule_date: dt.date,
    viewer: User | None = None,
    *,
    chat: bool = False,
) -> str:
    """Build the public schedule with counts in the bot and names in chat."""
    rows = (
        session.query(
            DriverScheduleMark.hour,
            User.id,
            User.vk_id,
            User.full_name,
        )
        .join(User, User.id == DriverScheduleMark.driver_id)
        .filter(DriverScheduleMark.schedule_date == schedule_date)
        .order_by(DriverScheduleMark.hour.asc(), User.full_name.asc(), User.id.asc())
        .all()
    )
    off_ids = {
        int(driver_id)
        for (driver_id,) in session.query(DriverScheduleOff.driver_id).filter(
            DriverScheduleOff.schedule_date == schedule_date
        ).all()
    }
    by_hour: dict[int, list[tuple[int, int, str]]] = {}
    for hour, driver_id, vk_id, full_name in rows:
        if int(driver_id) in off_ids:
            continue
        by_hour.setdefault(int(hour), []).append(
            (int(driver_id), int(vk_id), _display_name(int(driver_id), full_name))
        )
    lines = [f"🗓 График на {_day_title(schedule_date)}:", ""]
    if not by_hour:
        lines.append("Пока никто не отметил часы.")
    else:
        if chat:
            values = [
                (
                    hour,
                    tuple((vk_id, name) for _driver_id, vk_id, name in by_hour[hour]),
                )
                for hour in range(SLOT_COUNT)
                if hour in by_hour
            ]
            for start, end, names in _ranges(values):
                label = ", ".join(f"[id{vk_id}|{name}]" for vk_id, name in names)
                lines.append(f"• {_hour_label(start)}" if start == end else
                             f"• {_hour_label(start).split('–')[0]}–{_hour_label(end).split('–')[1]}")
                lines[-1] += f" — {label}"
        else:
            own_marks = marks_for(session, viewer, schedule_date) if viewer else set()
            values = [
                (hour, (len(by_hour[hour]), hour in own_marks))
                for hour in range(SLOT_COUNT)
                if hour in by_hour
            ]
            for start, end, value in _ranges(values):
                count, own = value
                prefix = "✅ " if own else "• "
                period = (
                    _hour_label(start)
                    if start == end
                    else f"{_hour_label(start).split('–')[0]}–{_hour_label(end).split('–')[1]}"
                )
                lines.append(
                    f"{prefix}{period} — {count} {_plural_drivers(count)}"
                )
    off_rows = (
        session.query(User.id, User.vk_id, User.full_name)
        .join(DriverScheduleOff, DriverScheduleOff.driver_id == User.id)
        .filter(DriverScheduleOff.schedule_date == schedule_date)
        .order_by(User.full_name.asc(), User.id.asc())
        .all()
    )
    if off_rows:
        if chat:
            names = ", ".join(
                _display_name(int(user_id), full_name)
                for _id, user_id, full_name in off_rows
            )
            lines.append(f"🚫 Не работают — {names}")
        else:
            lines.append(
                f"🚫 Не работают — {len(off_rows)} {_plural_drivers(len(off_rows))}"
            )
    dispatcher = _dispatcher_for_date(session, schedule_date)
    if dispatcher:
        _row, user = dispatcher
        lines.append(
            f"🎧 Диспетчер — {_mention(int(user.vk_id), user.full_name)}"
        )
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
    is_off = is_not_working(session, driver, schedule_date)
    token = _day_token(schedule_date)
    vk.send_message(
        driver.vk_id,
        schedule_text(session, schedule_date, driver),
        keyboard=kb.driver_schedule_day(token, bool(marks), is_off),
    )
    set_state(session, driver.vk_id, States.D_MENU)


def send_chat_schedule(
    session: Session,
    peer_id: int,
    day: str | None = None,
) -> None:
    """Reply to !график commands in the common driver conversation."""
    schedule_date = _date_for(day)
    vk.send_message(peer_id, schedule_text(session, schedule_date, chat=True))


def send_unmarked_chat(session: Session, peer_id: int) -> None:
    tomorrow = _today() + dt.timedelta(days=1)
    off_ids = {
        int(driver_id)
        for (driver_id,) in session.query(DriverScheduleOff.driver_id).filter(
            DriverScheduleOff.schedule_date == tomorrow
        ).all()
    }
    marked_ids = {
        int(driver_id)
        for (driver_id,) in session.query(DriverScheduleMark.driver_id).filter(
            DriverScheduleMark.schedule_date == tomorrow
        ).all()
    }
    drivers = (
        session.query(User)
        .filter(User.is_blocked.is_(False))
        .order_by(User.full_name.asc(), User.id.asc())
        .all()
    )
    names = [
        _mention(int(driver.id), driver.full_name)
        for driver in drivers
        if driver.has_role(ROLE_DRIVER)
        and driver.id not in off_ids
        and driver.id not in marked_ids
    ]
    text = (
        "✅ Все водители отметились или указали, что не работают завтра."
        if not names
        else "⚠️ Завтра не отметились:\n" + "\n".join(f"• {name}" for name in names)
    )
    vk.send_message(peer_id, text)


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
    is_off = is_not_working(session, driver, schedule_date)
    selected = len(marks)
    vk.send_message(
        driver.vk_id,
        f"✏️ Выберите часы на {_day_title(schedule_date)}.\n"
        f"Отмечено: {selected} из {SLOT_COUNT}.",
        keyboard=kb.driver_schedule_slots(token, marks, page, is_off),
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

    off_row = None
    if schedule_date == _today() + dt.timedelta(days=1):
        off_row = (
            session.query(DriverScheduleOff)
            .filter(
                DriverScheduleOff.driver_id == driver.id,
                DriverScheduleOff.schedule_date == schedule_date,
            )
            .one_or_none()
        )
        if off_row is not None:
            session.delete(off_row)

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


def is_not_working(
    session: Session,
    driver: User,
    schedule_date: dt.date,
) -> bool:
    return (
        session.query(DriverScheduleOff.id)
        .filter(
            DriverScheduleOff.driver_id == driver.id,
            DriverScheduleOff.schedule_date == schedule_date,
        )
        .first()
        is not None
    )


def toggle_not_working(
    session: Session,
    driver: User,
    day: str | None,
) -> None:
    schedule_date = _date_for(day)
    if schedule_date != _today() + dt.timedelta(days=1):
        return show_day(session, driver, "today")
    row = (
        session.query(DriverScheduleOff)
        .filter(
            DriverScheduleOff.driver_id == driver.id,
            DriverScheduleOff.schedule_date == schedule_date,
        )
        .one_or_none()
    )
    if row is None:
        session.add(DriverScheduleOff(
            driver_id=driver.id,
            schedule_date=schedule_date,
        ))
        session.query(DriverScheduleMark).filter(
            DriverScheduleMark.driver_id == driver.id,
            DriverScheduleMark.schedule_date == schedule_date,
        ).delete(synchronize_session=False)
    else:
        session.delete(row)
    session.flush()
    show_day(session, driver, "tomorrow")


def is_week_dispatcher(session: Session, dispatcher: User) -> bool:
    week = _dispatcher_assignment_week(_today())
    row = (
        session.query(DriverScheduleDispatcher)
        .filter(DriverScheduleDispatcher.week_start == week)
        .one_or_none()
    )
    return bool(row and row.dispatcher_id == dispatcher.id)


def claim_dispatcher_week(session: Session, dispatcher: User) -> None:
    if not dispatcher.has_role(ROLE_DISPATCHER):
        return vk.send_message(dispatcher.vk_id, "Недостаточно прав.")
    week = _dispatcher_assignment_week(_today())
    row = (
        session.query(DriverScheduleDispatcher)
        .filter(DriverScheduleDispatcher.week_start == week)
        .one_or_none()
    )
    if row is None:
        session.add(DriverScheduleDispatcher(
            dispatcher_id=dispatcher.id,
            week_start=week,
        ))
    else:
        row.dispatcher_id = dispatcher.id
    session.flush()
    vk.send_message(
        dispatcher.vk_id,
        "✅ Вы отмечены как диспетчер на неделю "
        f"с {week.strftime('%d.%m')} по {(week + dt.timedelta(days=6)).strftime('%d.%m')}.",
        keyboard=kb.dispatcher_menu(
            can_switch_role(dispatcher),
            dispatcher_schedule_active=True,
        ),
    )


def _reminder_drivers(session: Session, only_without_marks: bool) -> list[User]:
    tomorrow = _today() + dt.timedelta(days=1)
    off_ids = {
        int(driver_id)
        for (driver_id,) in session.query(DriverScheduleOff.driver_id).filter(
            DriverScheduleOff.schedule_date == tomorrow
        ).all()
    }
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
        if only_without_marks and (
            driver.id in off_ids or marks_for(session, driver, tomorrow)
        ):
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