"""Flask admin application factory + all routes.

Run locally:
    flask --app web.app run --debug
Or via gunicorn (bothost.ru / any VPS):
    gunicorn web.app:app
On Vercel it is imported by api/index.py as a serverless WSGI handler.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import secrets
import sys
import tempfile

# Ensure the project root is importable when the app is launched directly
# (e.g. `python web/app.py` or `gunicorn web.app:app` from inside web/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: E402

from flask import (  # noqa: E402
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import joinedload

from common.config import config
from common.database import get_session
from common.logger import get_logger
from common.models import (
    ROLE_DRIVER,
    ROLE_PASSENGER,
    AdminLog,
    AdminUser,
    BotMessage,
    Booking,
    LoginAttempt,
    BlockedUser,
    City,
    DriverQueue,
    DriverSelection,
    DispatcherCommission,
    FakeCall,
    Message,
    Order,
    OutboxMessage,
    PassengerQueue,
    Promocode,
    Promotion,
    Review,
    RotatingChatPost,
    ScheduledJob,
    State,
    User,
)
from common import price_service as ps
from common import bot_messages_service as bm
from common import time_utils
from common.settings_service import DEFAULTS, active_text_keys, ensure_defaults, get_all_settings, get_int, get_setting, set_setting

from . import broadcast
from .auth import login_required, verify_admin
from .excel_export import build_orders_workbook, build_stats_workbook
from .statistics_service import build_statistics
from .push_app import bp as push_app_bp

log = get_logger("web.app")

app = Flask(__name__)
if not config.SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be configured")
app.secret_key = config.SECRET_KEY
app.register_blueprint(push_app_bp)
from .driver_work import bp as driver_work_bp
app.register_blueprint(driver_work_bp)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_COOKIE_SECURE", "1") != "0",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

# Keep the DB schema in sync (auto-applies any pending Alembic migrations).
from common.db_migrate import ensure_schema  # noqa: E402

# Docker's migrate service has already completed before admin starts. Worker
# recycling must not rerun DDL/full-table rating repair during live traffic.
if os.getenv("AUTO_MIGRATE_ON_START", "1") != "0" and not ensure_schema():
    raise RuntimeError("Database schema migration failed")


# --------------------------------------------------------------------------- #
#  Request-scoped DB session                                                   #
# --------------------------------------------------------------------------- #
@app.before_request
def _open_session() -> None:
    request.db = get_session()  # type: ignore[attr-defined]


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def _security_context():
    return {"csrf_token": _csrf_token}


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; media-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.before_request
def _protect_post_requests():
    if request.method != "POST" or request.path.startswith("/api/driver-app/"):
        return None
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        abort(400, "Недействительный CSRF-токен")
    return None


@app.teardown_request
def _close_session(exc) -> None:
    db = getattr(request, "db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        db.close()


@app.get("/live")
def live():
    """HTTP liveness only. Database readiness remains a separate /health."""
    return {"status": "alive"}, 200


@app.get("/health")
def health():
    """BotHost health check: verifies both HTTP and PostgreSQL."""
    try:
        request.db.execute(text("SELECT 1"))  # type: ignore[attr-defined]
        return {"status": "ok"}, 200
    except Exception:
        return {"status": "database_unavailable"}, 503


def db():
    return request.db  # type: ignore[attr-defined]


@app.route("/healthz")
def healthz():
    """Unauthenticated deployment check for Vercel/Bothost diagnostics."""
    try:
        db().execute(text("SELECT 1"))
        return jsonify({"ok": True, "service": "taxi-admin"})
    except Exception as exc:  # noqa: BLE001
        log.error("Admin health check failed: %s", exc)
        return jsonify({"ok": False, "error": "database unavailable"}), 503


def log_action(action: str, details: str = "") -> None:
    admin_id = session.get("admin_id")
    db().add(AdminLog(admin_id=admin_id, action=action, details=details))
    db().commit()


# --------------------------------------------------------------------------- #
#  Auth                                                                        #
# --------------------------------------------------------------------------- #
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_value = request.form["login"].strip()
        ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()[:64]
        cutoff = time_utils.now() - dt.timedelta(minutes=15)
        try:
            failures = db().query(func.count(LoginAttempt.id)).filter(
                # Shared VPN/NAT addresses must not block unrelated accounts.
                # Keep the per-account limit across all IPs so switching a
                # VPN endpoint cannot bypass password-guessing protection.
                LoginAttempt.login == login_value,
                LoginAttempt.success.is_(False),
                LoginAttempt.created_at >= cutoff,
            ).scalar() or 0
        except ProgrammingError:
            # A serverless instance can briefly receive a request while its
            # deployment migration is still catching up. Create just this
            # security table and retry instead of returning a 500 login page.
            db().rollback()
            db().execute(text("""
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id SERIAL PRIMARY KEY, ip_address VARCHAR(64) NOT NULL,
                    login VARCHAR(120) NOT NULL,
                    success BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            db().execute(text("""
                CREATE INDEX IF NOT EXISTS ix_login_attempts_lookup
                ON login_attempts(ip_address, login, created_at DESC)
            """))
            db().commit()
            failures = 0
        if failures >= 5:
            return render_template("login.html", blocked=True), 429
        admin = verify_admin(db(), login_value, request.form["password"])
        db().add(LoginAttempt(ip_address=ip, login=login_value, success=bool(admin)))
        db().commit()
        if admin:
            session["admin_id"] = admin.id
            session["admin_login"] = admin.login
            log_action("login", f"admin {admin.login} вошёл")
            return redirect(url_for("dashboard"))
        flash("Неверный логин или пароль", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
#  Dashboard                                                                   #
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def dashboard():
    s = db()
    status_counts = dict(s.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    week_start = time_utils.now() - dt.timedelta(days=7)
    orders_7_days = s.query(func.count(Order.id)).filter(Order.created_at >= week_start).scalar() or 0
    total_users, total_drivers = s.query(
        func.count(User.id),
        func.count(User.id).filter(User.granted_roles.like("%driver%")),
    ).one()
    online_drivers = s.query(func.count(DriverQueue.id)).filter(DriverQueue.status == "waiting").scalar() or 0
    stats = {
        "total_users": total_users or 0,
        "total_drivers": total_drivers or 0,
        "online_drivers": online_drivers,
        "new_orders": sum(status_counts.get(x, 0) for x in ("created", "queued")),
        "waiting_driver": sum(status_counts.get(x, 0) for x in ("searching", "chat_search", "no_drivers")),
        "with_driver": sum(status_counts.get(x, 0) for x in ("assigned", "arrived", "in_progress")),
        "orders_7_days": orders_7_days,
    }
    return render_template("dashboard.html", stats=stats)


@app.route("/statistics")
@login_required
def statistics_view():
    now = time_utils.now()
    cutoff = now - dt.timedelta(days=14)
    orders = (
        db().query(Order)
        .options(joinedload(Order.driver))
        .filter(Order.created_at >= cutoff)
        .order_by(Order.created_at.asc())
        .all()
    )
    bookings = db().query(Booking).filter(Booking.created_at >= cutoff).all()
    stats = build_statistics(orders, bookings, now, time_utils.LOCAL_TZ)
    return render_template("statistics.html", stats=stats)


@app.route("/drivers")
@login_required
def drivers_view():
    """Weekly completed requests plus the full driver rating leaderboard."""
    from .statistics_service import build_driver_week_stats

    now = time_utils.now()
    start = (now - dt.timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    orders = (
        db().query(Order)
        .options(joinedload(Order.driver))
        .filter(Order.status == "completed")
        .filter(or_(Order.completed_at >= start, Order.created_at >= start))
        .all()
    )
    stats = build_driver_week_stats(orders, now, time_utils.LOCAL_TZ)
    all_drivers = (
        db().query(User)
        .filter(User.granted_roles.like("%driver%"))
        .all()
    )
    rating_rows = sorted(
        all_drivers,
        key=lambda driver: (
            -int(driver.rating_count or 0),
            -float(driver.rating or 0),
            (driver.full_name or f"id{driver.vk_id}").casefold(),
        ),
    )
    return render_template("drivers.html", stats=stats, rating_rows=rating_rows)


# --------------------------------------------------------------------------- #
#  Temporary drivers («Суточники»)                                            #
# --------------------------------------------------------------------------- #
def _queue_admin_user_notice(s, user: User, text_value: str, keyboard=None) -> None:
    """Queue a VK notice in the same transaction as an admin-panel change."""
    s.add(OutboxMessage(
        peer_id=user.vk_id,
        text=(text_value or "")[:4000],
        keyboard=keyboard,
        attachment=None,
        random_id=secrets.randbits(62) or 1,
        status="pending",
        priority=1000,
        attempts=0,
        next_attempt_at=time_utils.now(),
    ))


def _parse_local_admin_datetime(raw: str) -> dt.datetime | None:
    """Parse a datetime-local value as Asia/Yekaterinburg."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Укажите корректные дату и время начала")
    if parsed.tzinfo is not None:
        return parsed.astimezone(time_utils.LOCAL_TZ)
    timezone = time_utils.LOCAL_TZ
    return (
        timezone.localize(parsed)
        if hasattr(timezone, "localize")
        else parsed.replace(tzinfo=timezone)
    )


def _as_local_datetime(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize database datetimes before comparisons and sorting."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(time_utils.LOCAL_TZ)


def _temporary_driver_active_work(s, user: User) -> bool:
    """Never strip the role in the middle of a live offer or trip."""
    return s.query(Order.id).filter(or_(
        and_(
            Order.driver_id == user.id,
            Order.status.in_(("assigned", "arrived", "in_progress")),
        ),
        and_(
            Order.offered_driver_id == user.id,
            Order.driver_id.is_(None),
            Order.status == "searching",
        ),
        and_(
            Order.parallel_driver_id == user.id,
            Order.status == "parallel_assigned",
        ),
    )).first() is not None


def _reset_user_state_to_main(s, user: User) -> None:
    from bot.states_service import States

    state = s.query(State).filter(State.vk_id == user.vk_id).one_or_none()
    if state is None:
        state = State(vk_id=user.vk_id)
        s.add(state)
    state.state = States.MAIN_MENU
    state.data = "{}"


def _remove_temporary_driver_now(s, user: User) -> None:
    """Immediately and safely remove an idle temporary driver role."""
    from bot import keyboards as bot_keyboards
    from bot import outbox_service
    from bot.roles import can_switch_role

    selection = (
        s.query(DriverSelection)
        .filter(DriverSelection.driver_id == user.id)
        .with_for_update()
        .one_or_none()
    )
    if selection is not None:
        passenger = s.get(User, selection.passenger_id)
        if passenger is not None:
            _reset_user_state_to_main(s, passenger)
            _queue_admin_user_notice(
                s,
                passenger,
                "Выбранный водитель больше не свободен. Выберите другого водителя.",
                bot_keyboards.direct_driver_unavailable_keyboard(),
            )
        s.delete(selection)

    entry = (
        s.query(DriverQueue)
        .filter(DriverQueue.driver_id == user.id)
        .with_for_update()
        .one_or_none()
    )
    if entry is not None:
        if entry.front_notice_outbox_id:
            outbox_service.cancel_or_delete(s, int(entry.front_notice_outbox_id))
        s.delete(entry)

    user.is_on_line = False
    user.driver_status = "offline"
    was_active_driver = user.role == ROLE_DRIVER
    user.revoke_role(ROLE_DRIVER)
    if was_active_driver:
        user.role = ROLE_PASSENGER
    user.temporary_driver_starts_at = None
    user.temporary_driver_until = None
    _reset_user_state_to_main(s, user)
    _queue_admin_user_notice(
        s,
        user,
        "⛔ Администратор снял временную роль водителя."
        + (" Активная роль переключена на пассажира." if was_active_driver else ""),
        bot_keyboards.passenger_menu(can_switch_role(user)) if was_active_driver else None,
    )


@app.route("/temporary-drivers")
@login_required
def temporary_drivers_view():
    s = db()
    now = time_utils.now()
    users = (
        s.query(User)
        .filter(or_(
            User.temporary_driver_starts_at.isnot(None),
            User.temporary_driver_until.isnot(None),
        ))
        .all()
    )
    rows = []
    for user in users:
        starts_at = user.temporary_driver_starts_at
        until = user.temporary_driver_until
        local_start = _as_local_datetime(starts_at)
        local_until = _as_local_datetime(until)
        if starts_at is not None:
            state = "scheduled"
        elif local_until is not None and local_until <= now:
            state = "finishing"
        else:
            state = "active"
        rows.append({
            "user": user,
            "starts_at": starts_at,
            "until": until,
            "sort_at": local_start or local_until or now,
            "state": state,
        })
    state_order = {"active": 0, "scheduled": 1, "finishing": 2}
    rows.sort(key=lambda row: (
        state_order.get(row["state"], 9),
        row["sort_at"],
        (row["user"].full_name or f'id{row["user"].vk_id}').casefold(),
    ))
    return render_template(
        "temporary_drivers.html",
        rows=rows,
        active_count=sum(row["state"] == "active" for row in rows),
        scheduled_count=sum(row["state"] == "scheduled" for row in rows),
        finishing_count=sum(row["state"] == "finishing" for row in rows),
    )


@app.route("/temporary-drivers/create", methods=["POST"])
@login_required
def create_temporary_driver():
    s = db()
    vk_reference = request.form.get("vk_reference", "").strip()
    try:
        days = int(request.form.get("days", "1"))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= 365:
        flash("Срок должен быть от 1 до 365 дней", "danger")
        return redirect(url_for("temporary_drivers_view"))
    try:
        starts_at = _parse_local_admin_datetime(request.form.get("starts_at", ""))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("temporary_drivers_view"))
    now = time_utils.now()
    if starts_at is not None and starts_at - now > dt.timedelta(days=366):
        flash("Начало можно запланировать не более чем на год вперёд", "danger")
        return redirect(url_for("temporary_drivers_view"))

    vk_id, name = _resolve_vk_user(vk_reference)
    if not vk_id:
        flash("Не удалось определить VK ID. Укажите ссылку VK, @имя, id123 или числовой ID.", "danger")
        return redirect(url_for("temporary_drivers_view"))
    user = (
        s.query(User)
        .filter(User.vk_id == vk_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        user = User(
            vk_id=vk_id,
            full_name=name or f"id{vk_id}",
            role=ROLE_PASSENGER,
            granted_roles=ROLE_PASSENGER,
        )
        s.add(user)
        s.flush()
    elif name and (not user.full_name or user.full_name == f"id{vk_id}"):
        user.full_name = name

    if user.temporary_driver_starts_at is not None or user.temporary_driver_until is not None:
        flash("Этот пользователь уже является суточником или ожидает выдачу роли", "warning")
        return redirect(url_for("temporary_drivers_view"))
    if user.has_role(ROLE_DRIVER):
        flash("У пользователя уже есть постоянная роль водителя", "warning")
        return redirect(url_for("temporary_drivers_view"))

    effective_start = starts_at if starts_at and starts_at > now else now
    user.temporary_driver_until = effective_start + dt.timedelta(days=days)
    if starts_at and starts_at > now:
        user.temporary_driver_starts_at = starts_at
        notice = (
            "🕐 Администратор запланировал вам временную роль водителя.\n"
            f"Начало: {time_utils.format_local(starts_at)}.\n"
            f"Окончание: {time_utils.format_local(user.temporary_driver_until)}."
        )
        action = "schedule_temporary_driver"
        flash("Выдача роли суточника запланирована", "success")
    else:
        user.temporary_driver_starts_at = None
        user.grant_role(ROLE_DRIVER)
        notice = (
            "✅ Администратор назначил вам временную роль водителя.\n"
            f"Роль действует до {time_utils.format_local(user.temporary_driver_until)}.\n"
            "Напишите «Меню», чтобы выбрать роль водителя."
        )
        action = "create_temporary_driver"
        flash("Роль суточника выдана", "success")
    _queue_admin_user_notice(s, user, notice)
    s.commit()
    log_action(action, f"user={user.id} vk_id={user.vk_id} days={days}")
    return redirect(url_for("temporary_drivers_view"))


@app.route("/temporary-drivers/<int:user_id>/extend", methods=["POST"])
@login_required
def extend_temporary_driver(user_id):
    s = db()
    user = (
        s.query(User)
        .filter(User.id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None or user.temporary_driver_until is None:
        flash("Суточник не найден", "warning")
        return redirect(url_for("temporary_drivers_view"))
    try:
        days = int(request.form.get("days", "1"))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= 365:
        flash("Продление должно быть от 1 до 365 дней", "danger")
        return redirect(url_for("temporary_drivers_view"))
    now = time_utils.now()
    deadline = user.temporary_driver_until
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt.timezone.utc)
    user.temporary_driver_until = max(deadline, now) + dt.timedelta(days=days)
    _queue_admin_user_notice(
        s,
        user,
        f"🕐 Администратор продлил вашу временную роль водителя на {days} дн.\n"
        f"Новое окончание: {time_utils.format_local(user.temporary_driver_until)}.",
    )
    s.commit()
    log_action("extend_temporary_driver", f"user={user.id} days={days}")
    flash("Срок суточника продлён, пользователь уведомлён", "success")
    return redirect(url_for("temporary_drivers_view"))


@app.route("/temporary-drivers/<int:user_id>/cancel", methods=["POST"])
@login_required
def cancel_scheduled_temporary_driver(user_id):
    s = db()
    user = (
        s.query(User)
        .filter(User.id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None or user.temporary_driver_starts_at is None:
        flash("Запланированная выдача не найдена", "warning")
        return redirect(url_for("temporary_drivers_view"))
    starts = time_utils.format_local(user.temporary_driver_starts_at)
    user.temporary_driver_starts_at = None
    user.temporary_driver_until = None
    _queue_admin_user_notice(
        s,
        user,
        f"❌ Администратор отменил запланированную на {starts} выдачу временной роли водителя.",
    )
    s.commit()
    log_action("cancel_scheduled_temporary_driver", f"user={user.id}")
    flash("Запланированная выдача отменена, пользователь уведомлён", "success")
    return redirect(url_for("temporary_drivers_view"))


@app.route("/temporary-drivers/<int:user_id>/remove", methods=["POST"])
@login_required
def remove_temporary_driver(user_id):
    s = db()
    user = (
        s.query(User)
        .filter(User.id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None or user.temporary_driver_until is None or user.temporary_driver_starts_at is not None:
        flash("Активный суточник не найден", "warning")
        return redirect(url_for("temporary_drivers_view"))
    if _temporary_driver_active_work(s, user):
        user.temporary_driver_until = time_utils.now()
        _queue_admin_user_notice(
            s,
            user,
            "⏳ Администратор завершил срок временной роли водителя. "
            "Чтобы не прерывать активную заявку, роль будет снята сразу после её завершения.",
        )
        message = "Снятие назначено после завершения активной заявки, пользователь уведомлён"
    else:
        _remove_temporary_driver_now(s, user)
        message = "Роль суточника снята, пользователь уведомлён"
    s.commit()
    log_action("remove_temporary_driver", f"user={user.id}")
    flash(message, "success")
    return redirect(url_for("temporary_drivers_view"))


@app.route("/api/new-orders-count")
@login_required
def new_orders_count():
    """Lightweight endpoint polled by the dashboard for new-order notifications."""
    cnt = db().query(func.count(Order.id)).filter(
        Order.status.in_(["created", "searching"])
    ).scalar()
    return jsonify({"pending": cnt or 0})


# --------------------------------------------------------------------------- #
#  Users                                                                       #
# --------------------------------------------------------------------------- #
@app.route("/users")
@login_required
def users():
    s = db()
    role = request.args.get("role", "")
    q = request.args.get("q", "").strip()
    query = s.query(User)
    if role:
        query = query.filter(User.role == role)
    if q:
        query = query.filter(
            (User.full_name.ilike(f"%{q}%")) | (User.phone.ilike(f"%{q}%"))
        )
    users_list = query.order_by(User.created_at.desc()).limit(200).all()
    return render_template("users.html", users=users_list, role=role, q=q)


@app.route("/users/<int:user_id>/toggle-block", methods=["POST"])
@login_required
def toggle_block(user_id):
    s = db()
    user = s.get(User, user_id)
    if not user:
        abort(404)
    user.is_blocked = not user.is_blocked
    if user.is_blocked:
        if not s.query(BlockedUser).filter(BlockedUser.vk_id == user.vk_id).one_or_none():
            s.add(BlockedUser(vk_id=user.vk_id, reason="Заблокирован администратором"))
    else:
        s.query(BlockedUser).filter(BlockedUser.vk_id == user.vk_id).delete()
    s.commit()
    log_action("toggle_block", f"user {user_id} -> blocked={user.is_blocked}")
    flash("Статус блокировки обновлён", "success")
    return redirect(request.referrer or url_for("users"))


def _resolve_vk_user(link_or_name):
    """Requirement 8: resolve a VK link/username to (vk_id, full_name).

    Accepts vk.com/id123, vk.ru/durov, @durov, id123 or a raw numeric id.
    Screen names are resolved through the VK API (users.get)."""
    raw = (link_or_name or "").strip()
    if not raw:
        return None, None
    m = re.search(r"(?:m\.)?vk\.(?:com|ru)/([^/?#\s]+)", raw, re.IGNORECASE)
    token_str = (m.group(1) if m else raw).lstrip("@").strip()
    direct = None
    m2 = re.fullmatch(r"id(\d+)", token_str)
    if m2:
        direct = int(m2.group(1))
    elif token_str.isdigit():
        direct = int(token_str)
    user_ids = str(direct) if direct is not None else token_str
    if not config.VK_TOKEN:
        return direct, None
    try:
        resp = requests.get(
            "https://api.vk.com/method/users.get",
            params={
                "user_ids": user_ids,
                "fields": "first_name,last_name",
                "access_token": config.VK_TOKEN,
                "v": config.VK_API_VERSION,
            },
            timeout=10,
        )
        data = resp.json()
        items = data.get("response") or []
        if items:
            it = items[0]
            name = ("%s %s" % (it.get("first_name", ""), it.get("last_name", ""))).strip() or None
            return int(it["id"]), name
    except Exception as exc:  # noqa: BLE001
        log.error("VK id resolution failed for %s: %s", user_ids, exc)
    return direct, None


_ROLE_CHOICES = ("passenger", "driver", "dispatcher", "admin")


@app.route("/users/assign-role", methods=["POST"])
@login_required
def assign_role_by_link():
    """Requirement 8: grant a role to a user identified by a VK link."""
    s = db()
    link = request.form.get("vk_link", "").strip()
    role = request.form.get("role", "").strip()
    if role not in _ROLE_CHOICES:
        flash("Некорректная роль", "warning")
        return redirect(url_for("users"))
    vk_id, name = _resolve_vk_user(link)
    if not vk_id:
        flash("Не удалось определить VK ID. Укажите vk.com/id123 или короткое имя.", "danger")
        return redirect(url_for("users"))
    user = s.query(User).filter(User.vk_id == vk_id).one_or_none()
    if user is None:
        user = User(vk_id=vk_id, role="passenger", granted_roles="passenger")
        if name:
            user.full_name = name
        s.add(user)
        s.flush()
    elif name and not user.full_name:
        user.full_name = name
    user.grant_role(role)
    user.role = role
    s.commit()
    log_action("assign_role_by_link", "vk_id=%s role=%s" % (vk_id, role))
    flash("Роль «%s» назначена пользователю id%s" % (role, vk_id), "success")
    return redirect(url_for("users"))


@app.route("/users/revoke-role", methods=["POST"])
@login_required
def revoke_role_by_link():
    """Requirement 8: revoke a role from a user identified by a VK link."""
    s = db()
    link = request.form.get("vk_link", "").strip()
    role = request.form.get("role", "").strip()
    vk_id, _name = _resolve_vk_user(link)
    if not vk_id:
        flash("Не удалось определить VK ID.", "danger")
        return redirect(url_for("users"))
    user = s.query(User).filter(User.vk_id == vk_id).one_or_none()
    if user is None:
        flash("Пользователь не найден.", "warning")
        return redirect(url_for("users"))
    user.revoke_role(role)
    s.commit()
    log_action("revoke_role_by_link", "vk_id=%s role=%s" % (vk_id, role))
    flash("Роль «%s» снята у id%s" % (role, vk_id), "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/revoke-role", methods=["POST"])
@login_required
def revoke_user_role(user_id):
    s = db()
    user = s.get(User, user_id)
    if not user:
        abort(404)
    role = request.form.get("role", "")
    if role not in ("driver", "dispatcher") or not user.has_role(role):
        flash("Эту роль снять нельзя", "warning")
        return redirect(request.referrer or url_for("users"))
    user.revoke_role(role)
    s.commit()
    log_action("revoke_user_role", f"user {user_id} role={role}")
    flash("Роль снята", "success")
    return redirect(request.referrer or url_for("users"))


@app.route("/users/<int:user_id>/set-role", methods=["POST"])
@login_required
def set_role(user_id):
    s = db()
    user = s.get(User, user_id)
    if not user:
        abort(404)
    new_role = request.form["role"]
    user.grant_role(new_role)
    user.role = new_role
    s.commit()
    log_action("set_role", f"user {user_id} -> {user.role}")
    flash("Роль обновлена", "success")
    return redirect(request.referrer or url_for("users"))


@app.route("/users/<int:user_id>/reset-driver-block", methods=["POST"])
@login_required
def reset_driver_block(user_id):
    """Requirement 5: manually clear a driver's cancel-block + violation counter."""
    s = db()
    user = s.get(User, user_id)
    if not user:
        abort(404)
    user.driver_blocked_until = None
    user.driver_cancel_after_accept_count = 0
    user.driver_last_violation_at = None
    s.commit()
    log_action("reset_driver_block", f"user {user_id}")
    flash("Блокировка водителя сброшена", "success")
    return redirect(request.referrer or url_for("users"))


# --------------------------------------------------------------------------- #
#  All reviews (admin can delete any review)                                  #
# --------------------------------------------------------------------------- #
@app.route("/reviews")
@login_required
def reviews_all():
    s = db()
    kind = request.args.get("kind") or ""
    q = s.query(Review).order_by(Review.created_at.desc())
    if kind in ("passenger_to_driver", "driver_to_passenger"):
        q = q.filter(Review.kind == kind)
    rows = q.limit(500).all()
    items = []
    for r in rows:
        driver = s.get(User, r.driver_id) if r.driver_id else None
        passenger = s.get(User, r.passenger_id) if r.passenger_id else None
        if r.kind == "driver_to_passenger":
            author, target = driver, passenger
        else:
            author, target = passenger, driver
        items.append({
            "id": r.id,
            "kind": r.kind,
            "stars": r.stars or 0,
            "text": r.text or "",
            "created": time_utils.format_local(r.created_at) if r.created_at else "",
            "author": (author.full_name if author else None) or "-",
            "author_vk": author.vk_id if author else None,
            "target": (target.full_name if target else None) or "-",
            "target_vk": target.vk_id if target else None,
        })
    return render_template("reviews.html", items=items, kind=kind)


def _recalculate_rating_after_review_delete(s, review: Review) -> None:
    """Keep stored rating aggregates in sync with the remaining reviews."""
    if review.kind == "driver_to_passenger":
        target = s.get(User, review.passenger_id) if review.passenger_id else None
        query = s.query(
            func.coalesce(func.sum(Review.stars), 0),
            func.count(Review.id),
        ).filter(
            Review.kind == "driver_to_passenger",
            Review.passenger_id == review.passenger_id,
            Review.id != review.id,
        )
        stars_sum, stars_count = query.one()
        if target:
            target.passenger_rating_sum = int(stars_sum or 0)
            target.passenger_rating_count = int(stars_count or 0)
        return

    # Legacy/unknown kinds are passenger-to-driver reviews by default.
    target = s.get(User, review.driver_id) if review.driver_id else None
    query = s.query(
        func.coalesce(func.sum(Review.stars), 0),
        func.count(Review.id),
    ).filter(
        Review.kind == "passenger_to_driver",
        Review.driver_id == review.driver_id,
        Review.id != review.id,
    )
    stars_sum, stars_count = query.one()
    if target:
        target.rating_sum = int(stars_sum or 0)
        target.rating_count = int(stars_count or 0)
    if review.order_id:
        order = s.get(Order, review.order_id)
        if order:
            order.rating = None


@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@login_required
def delete_review(review_id):
    s = db()
    r = s.get(Review, review_id)
    if not r:
        abort(404)
    _recalculate_rating_after_review_delete(s, r)
    s.delete(r)
    s.commit()
    log_action("delete_review", f"review {review_id}")
    flash("Отзыв удалён, рейтинг пересчитан", "success")
    return redirect(request.referrer or url_for("reviews_all"))


# --------------------------------------------------------------------------- #
#  False calls (requirements 6-7)                                              #
# --------------------------------------------------------------------------- #
def _queue_fake_call_paid_notice(s, passenger: User) -> None:
    """Atomically unlock the passenger, reset the FSM and notify via bot outbox.

    The web process does not need a VK token: the bot's regular outbox worker
    delivers this row after the admin transaction commits.
    """
    from bot import keyboards as bot_keyboards
    from bot.roles import can_switch_role
    from bot.states_service import States

    state = s.query(State).filter(State.vk_id == passenger.vk_id).one_or_none()
    if state is None:
        state = State(vk_id=passenger.vk_id)
        s.add(state)
    state.state = States.MAIN_MENU
    state.data = "{}"
    s.add(OutboxMessage(
        peer_id=passenger.vk_id,
        text="Спасибо за оплату ложного вызова.",
        keyboard=bot_keyboards.passenger_menu(can_switch_role(passenger)),
        attachment=None,
        random_id=secrets.randbits(62) or 1,
        status="pending",
        priority=1000,
        attempts=0,
        next_attempt_at=time_utils.now(),
    ))


@app.route("/fake-calls")
@login_required
def fake_calls():
    s = db()
    status = request.args.get("status", "")
    query = s.query(FakeCall).options(joinedload(FakeCall.passenger), joinedload(FakeCall.driver))
    if status:
        query = query.filter(FakeCall.status == status)
    rows = query.order_by(FakeCall.created_at.desc()).limit(300).all()
    items = []
    for fc in rows:
        passenger = fc.passenger
        driver = fc.driver
        items.append({
            "fc": fc,
            "passenger": passenger,
            "driver": driver,
            "passenger_link": f"https://vk.com/id{passenger.vk_id}" if passenger else "",
            "driver_link": f"https://vk.com/id{driver.vk_id}" if driver else "",
        })
    return render_template("fake_calls.html", items=items, status=status)


@app.route("/fake-calls/<int:fc_id>/mark-paid", methods=["POST"])
@login_required
def fake_call_mark_paid(fc_id):
    s = db()
    fc = s.get(FakeCall, fc_id)
    if not fc:
        abort(404)
    if fc.status == "paid":
        flash("Ложный вызов уже отмечен оплаченным", "info")
        return redirect(request.referrer or url_for("fake_calls"))
    fc.status = "paid"
    fc.paid_at = time_utils.now()
    # The bot may still have an in-memory reminder, but it re-checks the paid
    # status before sending. Removing the persistent descriptor also prevents
    # the reminder from being restored after a restart.
    s.query(ScheduledJob).filter(
        ScheduledJob.job_key == f"fakecall:{fc.id}"
    ).delete(synchronize_session=False)
    passenger = s.get(User, fc.passenger_id)
    if passenger:
        other = s.query(FakeCall).filter(
            FakeCall.passenger_id == passenger.id,
            FakeCall.status == "pending",
            FakeCall.id != fc.id,
        ).first()
        if other is None:
            passenger.passenger_fake_call_blocked = False
            passenger.passenger_fake_call_blocked_until = None
            _queue_fake_call_paid_notice(s, passenger)
    s.commit()
    log_action("fake_call_mark_paid", f"fake_call {fc_id}")
    flash("Ложный вызов отмечен оплаченным", "success")
    return redirect(request.referrer or url_for("fake_calls"))


# --------------------------------------------------------------------------- #
#  Orders                                                                      #
# --------------------------------------------------------------------------- #
def _orders_query(s, status: str = "", q: str = ""):
    """Apply the Orders-tab filters without imposing its 200-row UI limit."""
    query = s.query(Order).options(
        joinedload(Order.passenger), joinedload(Order.driver)
    )
    if status:
        query = query.filter(Order.status == status)
    if q:
        # Every word must occur in one person's full name. Dispatcher-created
        # orders additionally use the free-form customer name.
        words = [word for word in q.split() if word][:8]
        passenger_name = and_(*(User.full_name.ilike(f"%{word}%") for word in words))
        driver_name = and_(*(User.full_name.ilike(f"%{word}%") for word in words))
        customer_name = and_(*(Order.customer_name.ilike(f"%{word}%") for word in words))
        query = query.filter(or_(
            Order.passenger.has(passenger_name),
            Order.driver.has(driver_name),
            customer_name,
        ))
    return query


def _bookings_query(s, status: str = "", q: str = ""):
    """Apply the Orders-tab filters to advance bookings."""
    query = s.query(Booking).options(
        joinedload(Booking.passenger), joinedload(Booking.driver)
    )
    if status:
        if status not in BOOKING_STATUS_LABELS:
            return query.filter(False)
        query = query.filter(Booking.status == status)
    if q:
        words = [word for word in q.split() if word][:8]
        passenger_name = and_(*(User.full_name.ilike(f"%{word}%") for word in words))
        driver_name = and_(*(User.full_name.ilike(f"%{word}%") for word in words))
        route_match = and_(*(Booking.route_text.ilike(f"%{word}%") for word in words))
        query = query.filter(or_(
            Booking.passenger.has(passenger_name),
            Booking.driver.has(driver_name),
            route_match,
        ))
    return query


@app.route("/orders")
@login_required
def orders():
    s = db()
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    orders_list = (
        _orders_query(s, status, q)
        .order_by(Order.created_at.desc())
        .limit(200)
        .all()
    )
    bookings_list = (
        _bookings_query(s, status, q)
        .order_by(Booking.created_at.desc())
        .limit(200)
        .all()
    )
    drivers = (
        s.query(User)
        .join(DriverQueue, DriverQueue.driver_id == User.id)
        .filter(
            User.granted_roles.like("%driver%"),
            User.is_blocked.is_(False),
            User.is_on_line.is_(True),
            User.driver_status == "online",
            DriverQueue.status == "waiting",
        )
        .order_by(DriverQueue.position.asc())
        .all()
    )
    return render_template(
        "orders.html",
        orders=orders_list,
        bookings=bookings_list,
        status=status,
        q=q,
        drivers=drivers,
    )


@app.route("/orders/export.xlsx")
@login_required
def export_orders():
    """Download every order and booking matching the current tab filters."""
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    rows = (
        _orders_query(db(), status, q)
        .order_by(Order.created_at.desc())
        .all()
    )
    booking_rows = (
        _bookings_query(db(), status, q)
        .order_by(Booking.created_at.desc())
        .all()
    )
    buffer = build_orders_workbook(rows, booking_rows)
    log_action(
        "export_orders",
        f"orders={len(rows)} bookings={len(booking_rows)} "
        f"status={status or 'all'} q={q or '—'}",
    )
    filename = f"orders_{time_utils.now().date().isoformat()}.xlsx"
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/orders/reset", methods=["POST"])
@login_required
def reset_orders():
    """Export and delete closed history while preserving all active work."""
    s = db()
    if request.form.get("confirmation") != "yes":
        flash("Сброс отменён: требуется подтверждение.", "danger")
        return redirect(url_for("orders"))

    # Lock both number-producing tables before checking their contents. This
    # prevents a bot worker from inserting a row between DELETE and sequence
    # restart and immediately moving the counter away from #1 again.
    if s.get_bind().dialect.name == "postgresql":
        s.execute(text("LOCK TABLE orders, bookings IN ACCESS EXCLUSIVE MODE"))

    active_order_statuses = (
        "created", "queued", "searching", "chat_search", "price_negotiation",
        "parallel_assigned", "assigned", "arrived", "in_progress",
    )
    active_booking_statuses = ("pending", "assigned", "driver_en_route")
    has_active_orders = s.query(Order.id).filter(
        Order.status.in_(active_order_statuses)
    ).first() is not None
    has_active_bookings = s.query(Booking.id).filter(
        Booking.status.in_(active_booking_statuses)
    ).first() is not None
    full_reset = not has_active_orders and not has_active_bookings

    closed_order_statuses = ("completed", "cancelled", "no_drivers")
    closed_booking_statuses = ("completed", "canceled")
    archived_orders = (
        s.query(Order)
        .options(joinedload(Order.passenger), joinedload(Order.driver))
        .filter(True if full_reset else Order.status.in_(closed_order_statuses))
        .order_by(Order.created_at.desc())
        .all()
    )
    archived_bookings = (
        s.query(Booking)
        .options(joinedload(Booking.passenger), joinedload(Booking.driver))
        .filter(True if full_reset else Booking.status.in_(closed_booking_statuses))
        .order_by(Booking.created_at.desc())
        .all()
    )

    # PostgreSQL sequences do not rewind when rows are deleted. This helper
    # must be defined before the empty-history branch: an administrator may
    # legitimately need to rewind counters after the rows were removed by an
    # earlier deployment whose reset returned too early.
    def reset_serial_counter(model) -> None:
        bind = s.get_bind()
        if bind.dialect.name != "postgresql":
            return
        sequence_name = s.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": model.__tablename__},
        ).scalar()
        if sequence_name:
            max_id = s.query(func.max(model.id)).scalar()
            if max_id is None:
                # Keep the internal key tidy when possible, but public_number
                # below is the actual user-visible counter and source of truth.
                s.execute(text(f"ALTER SEQUENCE {sequence_name} RESTART WITH 1"))
            else:
                s.execute(
                    text("SELECT setval(CAST(:sequence_name AS regclass), :max_id, true)"),
                    {"sequence_name": sequence_name, "max_id": int(max_id)},
                )

        # The visible number is deliberately separate from the primary key.
        # Reset this sequence as the source of truth for what people see.
        public_sequence = (
            "order_public_number_seq" if model is Order
            else "booking_public_number_seq"
        )
        public_max = s.query(func.max(model.public_number)).scalar()
        if public_max is None:
            s.execute(text(f"ALTER SEQUENCE {public_sequence} RESTART WITH 1"))
        else:
            s.execute(
                text("SELECT setval(CAST(:sequence_name AS regclass), :max_id, true)"),
                {"sequence_name": public_sequence, "max_id": int(public_max)},
            )

    if not archived_orders and not archived_bookings:
        # Previously this early return skipped both setval calls. As a result,
        # an already-empty orders/bookings table could still issue #4325.
        reset_serial_counter(Order)
        reset_serial_counter(Booking)
        s.commit()
        log_action("order_counters_reset", "no archived rows; counters synchronized")
        flash(
            "История уже пуста. Счётчики заявок и броней сброшены; следующая запись начнётся с #1.",
            "success",
        )
        return redirect(url_for("orders"))

    # Build the download entirely in memory before changing the database.
    # The POST response is this exact workbook, so one confirmed click both
    # archives and removes the same set of closed records.
    buffer = build_orders_workbook(archived_orders, archived_bookings)
    order_ids = [row.id for row in archived_orders]
    booking_ids = [row.id for row in archived_bookings]

    # Reviews and financial records are deliberately preserved. Only links to
    # the archived orders are detached before those orders are removed.
    s.query(Review).filter(Review.order_id.in_(order_ids)).update(
        {Review.order_id: None}, synchronize_session=False
    )
    s.query(DispatcherCommission).filter(DispatcherCommission.order_id.in_(order_ids)).update(
        {DispatcherCommission.order_id: None}, synchronize_session=False
    )
    s.query(FakeCall).filter(FakeCall.order_id.in_(order_ids)).update(
        {FakeCall.order_id: None}, synchronize_session=False
    )
    s.query(Order).filter(Order.parallel_offer_trip_id.in_(order_ids)).update(
        {Order.parallel_offer_trip_id: None}, synchronize_session=False
    )

    # An inconsistent old booking may still point to a closed order. Preserve
    # that booking and detach it unless the booking itself is also archived.
    s.query(Booking).filter(
        Booking.order_id.in_(order_ids),
        Booking.id.notin_(booking_ids),
    ).update({Booking.order_id: None}, synchronize_session=False)
    s.query(PassengerQueue).filter(PassengerQueue.order_id.in_(order_ids)).delete(
        synchronize_session=False
    )
    s.query(Message).filter(Message.order_id.in_(order_ids)).delete(
        synchronize_session=False
    )
    if booking_ids:
        s.query(Booking).filter(Booking.id.in_(booking_ids)).delete(
            synchronize_session=False
        )
    order_timer_kinds = (
        "accept", "waiting", "payment", "delivery", "fakecall", "pqueue",
        "pqueue_actual", "chat_actual", "driver_chat", "parallel_eta",
        "eta_prearrival", "dispatcher_unclaimed", "route_parallel_offer",
    )
    # Fake-call reminders are intentionally excluded: fake-call debts survive
    # the history reset and must continue notifying until paid.
    order_timer_kinds = tuple(kind for kind in order_timer_kinds if kind != "fakecall")
    if order_ids:
        s.query(ScheduledJob).filter(
            ScheduledJob.kind.in_(order_timer_kinds),
            ScheduledJob.object_id.in_(order_ids),
        ).delete(synchronize_session=False)
        s.query(Order).filter(Order.id.in_(order_ids)).delete(
            synchronize_session=False
        )
    if booking_ids:
        s.query(ScheduledJob).filter(
            ScheduledJob.kind == "booking_chat",
            ScheduledJob.object_id.in_(booking_ids),
        ).delete(synchronize_session=False)

    # With no active work, the reset is intentionally absolute: delete every
    # remaining order/booking row, including a legacy row with an unknown old
    # status that would otherwise keep #4677 alive and hold the sequence high.
    if full_reset:
        s.query(Booking).delete(synchronize_session=False)
        s.query(Order).delete(synchronize_session=False)
        s.flush()

    reset_serial_counter(Order)
    reset_serial_counter(Booking)
    s.commit()
    log_action(
        "orders_archive_reset",
        f"orders={len(order_ids)} bookings={len(booking_ids)}; active preserved",
    )
    filename = f"orders_archive_{time_utils.now().date().isoformat()}.xlsx"
    response = send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )
    response.headers["X-Archived-Orders"] = str(len(order_ids))
    response.headers["X-Archived-Bookings"] = str(len(booking_ids))
    return response


@app.route("/orders/<int:order_id>/assign", methods=["POST"])
@login_required
def assign_driver(order_id):
    s = db()
    driver_id = request.form.get("driver_id")
    if driver_id:
        try:
            parsed_driver_id = int(driver_id)
        except (TypeError, ValueError):
            parsed_driver_id = 0
        from bot.admin_order_assign_service import assign_from_admin

        if assign_from_admin(s, order_id, parsed_driver_id):
            s.commit()
            log_action("assign_driver", f"order {order_id} -> driver {driver_id}")
            flash("Водитель назначен", "success")
        else:
            s.rollback()
    return redirect(url_for("orders"))


@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
def cancel_order(order_id):
    s = db()
    order = (
        s.query(Order)
        .filter(Order.id == order_id)
        .with_for_update()
        .one_or_none()
    )
    if not order:
        abort(404)
    reason = request.form.get("reason", "").strip()
    from bot.admin_order_cancel_service import cancel_from_admin

    result = cancel_from_admin(s, order, reason)
    s.commit()
    log_action(
        "cancel_order",
        f"order {order_id} reason={reason or '—'} "
        f"previous={result.get('previous_status', order.status)} "
        f"messages={result.get('cancelled_messages', 0)}",
    )
    if result.get("changed"):
        flash(
            "Заказ отменён. Предложения водителям удалены"
            + (f" ({reason})" if reason else ""),
            "success",
        )
    else:
        flash("Заказ уже был завершён или отменён", "warning")
    return redirect(url_for("orders"))


@app.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    """Cancel an active booking and all of its bot-side effects."""
    s = db()
    booking = (
        s.query(Booking)
        .filter(Booking.id == booking_id)
        .with_for_update()
        .one_or_none()
    )
    if booking is None:
        abort(404)
    from bot.admin_booking_cancel_service import cancel_from_admin

    result = cancel_from_admin(s, booking)
    s.commit()
    log_action(
        "cancel_booking",
        f"booking {booking_id} previous={result.get('previous_status')} "
        f"linked_order={int(bool(result.get('linked_order_cancelled')))}",
    )
    if result.get("changed"):
        flash(f"Бронь №{booking_id} отменена", "success")
    else:
        flash("Бронь уже была отменена или завершена", "warning")
    return redirect(request.referrer or url_for("orders"))


# --------------------------------------------------------------------------- #
#  Giveaway                                                                   #
# --------------------------------------------------------------------------- #
def _giveaway_boundary(raw: str, *, end: bool = False) -> dt.datetime | None:
    """Convert an HTML date to an aware local inclusive/exclusive boundary."""
    if not raw:
        return None
    day = dt.date.fromisoformat(raw)
    if end:
        day += dt.timedelta(days=1)
    naive = dt.datetime.combine(day, dt.time.min)
    local_tz = time_utils.LOCAL_TZ
    if hasattr(local_tz, "localize"):
        return local_tz.localize(naive)
    return naive.replace(tzinfo=local_tz)


def _giveaway_form() -> dict:
    posted = request.method == "POST"
    return {
        "contest_text": request.form.get("contest_text", "").strip(),
        "prizes": request.form.get("prizes", "").strip(),
        "winners_count": request.form.get("winners_count", "1").strip(),
        "date_from": request.form.get("date_from", "").strip(),
        "date_to": request.form.get("date_to", "").strip(),
        "min_completed": request.form.get("min_completed", "2").strip(),
        "min_rating": request.form.get("min_rating", "4.0").strip(),
        "condition_order": request.form.get("condition_order") == "1" if posted else True,
        "condition_completed": request.form.get("condition_completed") == "1" if posted else True,
        "condition_min_completed": request.form.get("condition_min_completed") == "1",
        "condition_no_cancelled": request.form.get("condition_no_cancelled") == "1",
        "condition_phone": request.form.get("condition_phone") == "1",
        "condition_verified": request.form.get("condition_verified") == "1",
        "condition_rating": request.form.get("condition_rating") == "1",
    }


def _giveaway_candidates(s, form: dict) -> list[dict]:
    """Return unique unblocked passenger profiles matching all checked rules."""
    order_scope = [Order.dispatcher_id.is_(None)]
    start = _giveaway_boundary(form["date_from"])
    end = _giveaway_boundary(form["date_to"], end=True)
    if start:
        order_scope.append(Order.created_at >= start)
    if end:
        order_scope.append(Order.created_at < end)

    order_stats = (
        s.query(
            Order.passenger_id.label("participant_id"),
            func.count(Order.id).label("order_count"),
            func.sum(case((Order.status == "completed", 1), else_=0)).label("completed_count"),
            func.sum(case((Order.status == "cancelled", 1), else_=0)).label("cancelled_count"),
            func.max(Order.created_at).label("last_order_at"),
        )
        .filter(*order_scope)
        .group_by(Order.passenger_id)
        .subquery()
    )
    order_count = func.coalesce(order_stats.c.order_count, 0)
    completed_count = func.coalesce(order_stats.c.completed_count, 0)
    cancelled_count = func.coalesce(order_stats.c.cancelled_count, 0)
    query = (
        s.query(
            User,
            order_count.label("order_count"),
            completed_count.label("completed_count"),
            cancelled_count.label("cancelled_count"),
            order_stats.c.last_order_at,
        )
        .outerjoin(order_stats, order_stats.c.participant_id == User.id)
        .filter(User.is_blocked.is_(False))
    )
    if form["condition_order"]:
        query = query.filter(order_count >= 1)
    if form["condition_completed"]:
        query = query.filter(completed_count >= 1)
    if form["condition_min_completed"]:
        query = query.filter(completed_count >= int(form["min_completed"]))
    if form["condition_no_cancelled"]:
        query = query.filter(cancelled_count == 0)
    if form["condition_phone"]:
        query = query.filter(func.length(func.trim(func.coalesce(User.phone, ""))) > 0)
    if form["condition_verified"]:
        query = query.filter(User.verify_status == "passed")
    if form["condition_rating"]:
        query = query.filter(
            User.passenger_rating_count > 0,
            (
                User.passenger_rating_sum
                * 1.0
                / func.nullif(User.passenger_rating_count, 0)
            )
            >= float(form["min_rating"]),
        )

    candidates = []
    for user, total, completed, cancelled, last_order_at in query.order_by(User.id).all():
        candidates.append({
            "id": user.id,
            "vk_id": user.vk_id,
            "vk_url": "https://vk.com/id%s" % user.vk_id,
            "name": user.full_name or f"id{user.vk_id}",
            "phone": user.phone or "—",
            "orders": int(total or 0),
            "completed": int(completed or 0),
            "cancelled": int(cancelled or 0),
            "rating": user.passenger_rating,
            "last_order_at": last_order_at,
        })
    return candidates


@app.route("/giveaway", methods=["GET", "POST"])
@login_required
def giveaway_view():
    form = _giveaway_form()
    candidates: list[dict] = []
    winners: list[dict] = []
    result_text = ""
    if request.method == "POST":
        try:
            winners_count = max(1, min(50, int(form["winners_count"])))
            min_completed = max(1, min(1000, int(form["min_completed"])))
            min_rating = max(1.0, min(5.0, float(form["min_rating"])))
            form["winners_count"] = str(winners_count)
            form["min_completed"] = str(min_completed)
            form["min_rating"] = f"{min_rating:.1f}"
            if form["date_from"] and form["date_to"]:
                if dt.date.fromisoformat(form["date_from"]) > dt.date.fromisoformat(form["date_to"]):
                    raise ValueError("Дата начала позже даты окончания")
        except (TypeError, ValueError) as exc:
            flash(f"Проверьте числовые значения и даты: {exc}", "warning")
            return render_template("giveaway.html", form=form, candidates=[], winners=[], result_text="")

        candidates = _giveaway_candidates(db(), form)
        if request.form.get("action") == "draw":
            prizes = [line.strip() for line in form["prizes"].splitlines() if line.strip()]
            if not form["contest_text"]:
                flash("Введите текст розыгрыша", "warning")
            elif not prizes:
                flash("Укажите хотя бы один приз", "warning")
            elif len(prizes) not in (1,) and len(prizes) < winners_count:
                flash("Укажите по одному призу для каждого победителя или один общий приз", "warning")
            elif len(candidates) < winners_count:
                flash(
                    f"Под условия подходят только {len(candidates)} человек, а победителей указано {winners_count}",
                    "warning",
                )
            else:
                chosen = secrets.SystemRandom().sample(candidates, winners_count)
                for index, winner in enumerate(chosen):
                    winner = dict(winner)
                    winner["place"] = index + 1
                    winner["prize"] = prizes[0] if len(prizes) == 1 else prizes[index]
                    winners.append(winner)
                lines = ["🎉 ИТОГИ РОЗЫГРЫША", "", form["contest_text"], "", "🏆 Победители:"]
                for winner in winners:
                    lines.append(
                        f"{winner['place']}. [id{winner['vk_id']}|{winner['name']}] — {winner['prize']}"
                    )
                lines.extend(["", "Поздравляем победителей! 🎁"])
                result_text = "\n".join(lines)
                log_action(
                    "giveaway_draw",
                    "eligible=%s winners=%s ids=%s" % (
                        len(candidates), winners_count,
                        ",".join(str(item["id"]) for item in winners),
                    ),
                )
    return render_template(
        "giveaway.html",
        form=form,
        candidates=candidates,
        winners=winners,
        result_text=result_text,
    )


# --------------------------------------------------------------------------- #
#  Broadcast                                                                   #
# --------------------------------------------------------------------------- #
@app.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast_view():
    if request.method == "POST":
        text = request.form["text"].strip()
        target = request.form.get("target", "all")
        attachment = request.form.get("attachment", "").strip() or None
        if not text:
            flash("Введите текст рассылки", "warning")
        else:
            job_id = broadcast.start_broadcast(text, target, attachment)
            log_action("broadcast", f"target={target}")
            flash("Рассылка запущена в фоновом режиме", "success")
            return redirect(url_for("broadcast_status", job_id=job_id))
    return render_template("broadcast.html")


@app.route("/broadcast/status/<job_id>")
@login_required
def broadcast_status(job_id):
    job = broadcast.get_job(job_id)
    return render_template("broadcast.html", job=job, job_id=job_id)


@app.route("/api/broadcast/<job_id>")
@login_required
def broadcast_progress(job_id):
    job = broadcast.get_job(job_id)
    if not job:
        return jsonify({"found": False})
    return jsonify({
        "found": True, "total": job.total, "sent": job.sent,
        "failed": job.failed, "done": job.done, "error": job.error,
    })


# --------------------------------------------------------------------------- #
#  Rotating community-chat posts                                               #
# --------------------------------------------------------------------------- #
_VK_ROTATING_MEDIA = re.compile(
    r"(?:^|/)((?:photo|video)-?\d+_\d+(?:_[A-Za-z0-9]+)?)$",
    re.IGNORECASE,
)


def _normalize_rotating_media(raw: str) -> str | None:
    values = []
    for item in re.split(r"[\s,]+", (raw or "").strip()):
        if not item:
            continue
        match = _VK_ROTATING_MEDIA.search(item.rstrip("/"))
        if not match:
            raise ValueError("Допустимы только VK-фото и VK-видео")
        value = match.group(1)
        if value not in values:
            values.append(value)
    if len(values) > 10:
        raise ValueError("К одному посту можно прикрепить не более 10 медиа")
    return ",".join(values) or None


def _upload_rotating_photo() -> str | None:
    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return None
    suffix = os.path.splitext(photo.filename)[1].lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        raise ValueError("Фото должно быть в формате JPG, PNG или WEBP")
    peer_id = get_int(db(), "rotating_posts_peer_id", 0)
    if peer_id <= 2_000_000_000:
        raise ValueError("Сначала сохраните корректный peer_id чата")
    temp_path = ""
    normalized_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            photo.save(temp_file)
            temp_path = temp_file.name
        try:
            with Image.open(temp_path) as image:
                image.verify()
                if image.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Фото должно быть в формате JPG, PNG или WEBP")
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Не удалось прочитать файл фотографии") from exc
        # VK upload servers are most reliable with a regular RGB JPEG. This
        # also removes unsupported color profiles/alpha channels and prevents
        # valid browser images from producing VK's "photo is undefined".
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as normalized:
            normalized_path = normalized.name
        try:
            with Image.open(temp_path) as source:
                source = ImageOps.exif_transpose(source)
                if source.mode in ("RGBA", "LA"):
                    rgba = source.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, "white")
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    source = flattened
                else:
                    source = source.convert("RGB")
                source.save(
                    normalized_path,
                    format="JPEG",
                    quality=92,
                    optimize=True,
                )
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("Не удалось подготовить фотографию для VK") from exc
        from bot.vk_client import vk

        return vk.upload_message_photo(peer_id, normalized_path)
    finally:
        for path in (temp_path, normalized_path):
            if not path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass


def _rotating_post_form_values():
    text_value = (request.form.get("text") or "").strip()[:4000]
    attachments = _normalize_rotating_media(request.form.get("attachments") or "")
    attachment_values = attachments.split(",") if attachments else []
    if request.files.get("photo") and len(attachment_values) >= 10:
        raise ValueError("К одному посту можно прикрепить не более 10 медиа")
    uploaded_photo = _upload_rotating_photo()
    if uploaded_photo and uploaded_photo not in attachment_values:
        attachment_values.append(uploaded_photo)
    attachments = ",".join(attachment_values) or None
    try:
        position = int(request.form.get("position") or 0)
    except (TypeError, ValueError):
        position = 0
    position = max(-100_000, min(100_000, position))
    if not text_value and not attachments:
        raise ValueError("Добавьте текст или медиа")
    return text_value, attachments, position


@app.route("/rotating-posts")
@login_required
def rotating_posts_view():
    posts = (
        db().query(RotatingChatPost)
        .order_by(RotatingChatPost.position.asc(), RotatingChatPost.id.asc())
        .all()
    )
    return render_template(
        "rotating_posts.html",
        posts=posts,
        enabled=str(get_setting(db(), "rotating_posts_enabled", "0")) == "1",
        peer_id=get_setting(db(), "rotating_posts_peer_id", "0"),
        interval=get_int(db(), "rotating_posts_interval_minutes", 15),
        last_post_id=get_int(db(), "rotating_posts_last_post_id", 0),
        last_sent_at=get_setting(db(), "rotating_posts_last_sent_at", ""),
    )


@app.route("/rotating-posts/settings", methods=["POST"])
@login_required
def rotating_posts_settings():
    s = db()
    enabled = request.form.get("enabled") == "1"
    try:
        peer_id = int(request.form.get("peer_id") or 0)
        interval = int(request.form.get("interval") or 15)
    except (TypeError, ValueError):
        flash("Проверьте peer_id и интервал", "warning")
        return redirect(url_for("rotating_posts_view"))
    if (enabled or peer_id) and peer_id <= 2_000_000_000:
        flash("Для чата укажите peer_id больше 2000000000", "warning")
        return redirect(url_for("rotating_posts_view"))
    if interval < 1:
        flash("Интервал должен быть не меньше 1 минуты", "warning")
        return redirect(url_for("rotating_posts_view"))
    previous_enabled = str(get_setting(s, "rotating_posts_enabled", "0")) == "1"
    previous_peer = get_int(s, "rotating_posts_peer_id", 0)
    set_setting(s, "rotating_posts_peer_id", str(peer_id))
    set_setting(s, "rotating_posts_interval_minutes", str(interval))
    set_setting(s, "rotating_posts_enabled", "1" if enabled else "0")
    if enabled and (not previous_enabled or previous_peer != peer_id):
        set_setting(s, "rotating_posts_last_sent_at", time_utils.now().isoformat())
        set_setting(s, "rotating_posts_last_post_id", "0")
    s.commit()
    log_action(
        "rotating_posts_settings",
        f"enabled={int(enabled)} peer_id={peer_id} interval={interval}",
    )
    flash("Настройки автопубликации сохранены", "success")
    return redirect(url_for("rotating_posts_view"))


@app.route("/rotating-posts/check", methods=["POST"])
@login_required
def rotating_posts_check():
    try:
        peer_id = int(request.form.get("peer_id") or 0)
    except (TypeError, ValueError):
        peer_id = 0
    from bot.vk_client import vk

    available, details = vk.check_community_chat_access(peer_id)
    if available:
        suffix = f": {details}" if details else ""
        flash(f"Токен сообщества имеет доступ к чату{suffix}", "success")
    else:
        flash(f"Нет доступа к чату: {details}", "danger")
    return redirect(url_for("rotating_posts_view"))


@app.route("/rotating-posts/create", methods=["POST"])
@login_required
def rotating_post_create():
    s = db()
    try:
        text_value, attachments, position = _rotating_post_form_values()
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("rotating_posts_view"))
    post = RotatingChatPost(
        text=text_value,
        attachments=attachments,
        position=position,
        is_active=request.form.get("is_active") == "1",
    )
    s.add(post)
    s.commit()
    log_action("rotating_post_create", f"post={post.id}")
    flash("Пост добавлен в ротацию", "success")
    return redirect(url_for("rotating_posts_view"))


@app.route("/rotating-posts/<int:post_id>/update", methods=["POST"])
@login_required
def rotating_post_update(post_id: int):
    s = db()
    post = s.get(RotatingChatPost, post_id)
    if post is None:
        abort(404)
    try:
        text_value, attachments, position = _rotating_post_form_values()
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("rotating_posts_view"))
    post.text = text_value
    post.attachments = attachments
    post.position = position
    post.is_active = request.form.get("is_active") == "1"
    s.commit()
    log_action("rotating_post_update", f"post={post.id}")
    flash("Пост сохранён", "success")
    return redirect(url_for("rotating_posts_view"))


@app.route("/rotating-posts/<int:post_id>/delete", methods=["POST"])
@login_required
def rotating_post_delete(post_id: int):
    s = db()
    post = s.get(RotatingChatPost, post_id)
    if post is not None:
        s.delete(post)
        s.commit()
        log_action("rotating_post_delete", f"post={post_id}")
    return redirect(url_for("rotating_posts_view"))


# --------------------------------------------------------------------------- #
#  Promotions + Promocodes                                                     #
# --------------------------------------------------------------------------- #
@app.route("/promotions")
@login_required
def promotions():
    s = db()
    promos = s.query(Promotion).order_by(Promotion.created_at.desc()).all()
    codes = s.query(Promocode).order_by(Promocode.id.desc()).all()
    return render_template("promotions.html", promos=promos, codes=codes)


@app.route("/promotions/create", methods=["POST"])
@login_required
def create_promotion():
    s = db()
    s.add(Promotion(title=request.form["title"], text=request.form["text"]))
    s.commit()
    log_action("create_promotion", request.form["title"])
    flash("Акция создана", "success")
    return redirect(url_for("promotions"))


@app.route("/promotions/<int:promo_id>/delete", methods=["POST"])
@login_required
def delete_promotion(promo_id):
    s = db()
    obj = s.get(Promotion, promo_id)
    if obj:
        s.delete(obj)
        s.commit()
        log_action("delete_promotion", str(promo_id))
    return redirect(url_for("promotions"))


@app.route("/promocodes/create", methods=["POST"])
@login_required
def create_promocode():
    s = db()
    valid_until = request.form.get("valid_until") or None
    code = Promocode(
        code=request.form["code"].strip().upper(),
        discount=float(request.form["discount"]),
        discount_type=request.form.get("discount_type", "percent"),
        usage_limit=int(request.form["usage_limit"]) if request.form.get("usage_limit") else None,
        valid_until=dt.datetime.fromisoformat(valid_until) if valid_until else None,
    )
    s.add(code)
    s.commit()
    log_action("create_promocode", code.code)
    flash("Промокод создан", "success")
    return redirect(url_for("promotions"))


@app.route("/promocodes/<int:code_id>/delete", methods=["POST"])
@login_required
def delete_promocode(code_id):
    s = db()
    obj = s.get(Promocode, code_id)
    if obj:
        s.delete(obj)
        s.commit()
        log_action("delete_promocode", str(code_id))
    return redirect(url_for("promotions"))


# --------------------------------------------------------------------------- #
#  Settings                                                                    #
# --------------------------------------------------------------------------- #
_ADMIN_NUMBER_LIMITS = {
    "free_waiting_minutes": (0, 60),
    "price_per_waiting_minute": (0, 10_000),
    "svc_baggage_price": (0, 100_000),
    "svc_animal_price": (0, 100_000),
    "night_surcharge_amount": (0, 100_000),
    "night_start_hour": (0, 23),
    "night_end_hour": (0, 23),
    "driver_accept_timeout": (10, 600),
    "delivery_confirm_timeout": (10, 1_800),
    "passenger_cancel_grace_seconds": (0, 3_600),
}
_ADMIN_DECIMAL_SETTINGS = {
    "price_per_waiting_minute",
    "svc_baggage_price",
    "svc_animal_price",
    "night_surcharge_amount",
}


def _validated_admin_setting(key: str, value: str) -> str | None:
    """Normalize settings exposed by the existing numeric admin controls."""
    bounds = _ADMIN_NUMBER_LIMITS.get(key)
    if bounds is None:
        return value
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if key not in _ADMIN_DECIMAL_SETTINGS and not parsed.is_integer():
        return None
    minimum, maximum = bounds
    normalized = max(float(minimum), min(float(maximum), parsed))
    return f"{normalized:g}" if key in _ADMIN_DECIMAL_SETTINGS else str(int(normalized))


@app.route("/bot-texts")
@login_required
def bot_texts_view():
    """Searchable, paginated editor containing only active outgoing texts."""
    s = db()
    bm.ensure_defaults(s)
    s.commit()
    query = (request.args.get("q") or "").strip().casefold()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 40
    items = [{
        "kind": "bot", "key": row.key, "title": bm.title_for(row.key),
        "text": row.text or "", "default": bm.DEFAULTS[row.key][1],
    } for row in s.query(BotMessage).order_by(BotMessage.key.asc()).all()]
    items.extend({
        "kind": "setting", "key": key, "title": key,
        "text": get_setting(s, key, DEFAULTS[key]), "default": DEFAULTS[key],
    } for key in active_text_keys())
    items = [item for item in items if not query
             or query in item["key"].casefold()
             or query in item["title"].casefold()
             or query in item["text"].casefold()]
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    visible = items[(page - 1) * per_page:page * per_page]
    return render_template(
        "bot_texts.html",
        items=visible,
        q=request.args.get("q", ""),
        page=page,
        pages=pages,
        total=total,
    )


@app.route("/bot-texts/<kind>/<path:key>/update", methods=["POST"])
@login_required
def update_bot_text(kind: str, key: str):
    if kind == "bot" and key not in bm.DEFAULTS:
        abort(404)
    if kind == "setting" and key not in active_text_keys():
        abort(404)
    s = db()
    value = request.form.get("text", "")[:4000]
    if kind == "bot":
        bm.set_message(s, key, text=value)
    else:
        set_setting(s, key, value)
    s.commit()
    log_action("update_bot_text", key)
    flash("Текст сохранён", "success")
    return redirect(url_for(
        "bot_texts_view",
        q=request.form.get("q", ""),
        page=request.form.get("page", "1"),
    ))


@app.route("/bot-texts/<kind>/<path:key>/reset", methods=["POST"])
@login_required
def reset_bot_text(kind: str, key: str):
    if kind == "bot" and key not in bm.DEFAULTS:
        abort(404)
    if kind == "setting" and key not in active_text_keys():
        abort(404)
    s = db()
    if kind == "bot":
        bm.set_message(s, key, text=bm.DEFAULTS[key][1])
    else:
        set_setting(s, key, DEFAULTS[key])
    s.commit()
    log_action("reset_bot_text", key)
    flash("Возвращён исходный текст", "info")
    return redirect(url_for(
        "bot_texts_view",
        q=request.form.get("q", ""),
        page=request.form.get("page", "1"),
    ))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    s = db()
    ensure_defaults(s)
    s.commit()
    if request.method == "POST":
        allowed = set(DEFAULTS)
        changed = []
        for key, value in request.form.items():
            if key not in allowed:
                continue
            value = _validated_admin_setting(key, value)
            if value is None:
                continue
            old_value = get_setting(s, key)
            if str(old_value or "") != value:
                set_setting(s, key, value)
                changed.append(key)
        s.commit()
        if changed:
            log_action("update_settings", "изменено: " + ", ".join(changed))
            flash("Сохранено: " + ", ".join(changed), "success")
        else:
            flash("Изменений нет", "info")
        return redirect(url_for("settings_view"))
    current = get_all_settings(s)
    cities = s.query(City).order_by(City.name).all()
    return render_template("settings.html", settings=current, cities=cities)


@app.route("/cities/create", methods=["POST"])
@login_required
def create_city():
    s = db()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Название города не может быть пустым", "danger")
        return redirect(url_for("settings_view"))
    existing = s.query(City).filter(func.lower(City.name) == name.lower()).one_or_none()
    if existing:
        existing.is_active = True
        flash("Город уже существовал и снова включён", "info")
    else:
        s.add(City(name=name, is_active=True))
    s.commit()
    log_action("create_city", name)
    return redirect(url_for("settings_view"))


@app.route("/cities/<int:city_id>/update", methods=["POST"])
@login_required
def update_city(city_id: int):
    """Rename/toggle a recognition city and keep denormalized line names valid."""
    s = db()
    city = s.get(City, city_id)
    if city is None:
        abort(404)
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("Название города не может быть пустым", "danger")
        return redirect(url_for("settings_view"))
    duplicate = (
        s.query(City)
        .filter(City.id != city.id, func.lower(City.name) == new_name.lower())
        .one_or_none()
    )
    if duplicate:
        flash("Город с таким названием уже существует", "danger")
        return redirect(url_for("settings_view"))

    old_name = city.name
    city.name = new_name
    city.is_active = request.form.get("is_active") == "1"
    # Driver/current order fields intentionally keep the human-readable line
    # name, so update them atomically with the city row.
    s.query(User).filter(User.current_line == old_name).update(
        {User.current_line: new_name}, synchronize_session=False
    )
    s.query(Order).filter(Order.line == old_name).update(
        {Order.line: new_name}, synchronize_session=False
    )
    s.query(Order).filter(Order.pickup_city == old_name).update(
        {Order.pickup_city: new_name}, synchronize_session=False
    )
    s.commit()
    log_action("update_city", f"{old_name} -> {new_name}; active={city.is_active}")
    flash("Город сохранён", "success")
    return redirect(url_for("settings_view"))


# --------------------------------------------------------------------------- #
#  Price sections («Прайс» -> «Самые популярные направления»)                  #
# --------------------------------------------------------------------------- #
@app.route("/price-sections", methods=["GET", "POST"])
@login_required
def price_sections_view():
    s = db()
    ps.ensure_defaults(s)
    if request.method == "POST":
        key = request.form.get("section_key", "")
        if key not in ps.all_keys():
            abort(404)
        new_title = request.form.get("title", "").strip()
        new_content = request.form.get("content", "").strip()
        # Capture the previous values so we can detect whether a line changed
        # and only then notify the drivers (avoids spam on a no-op save).
        old = ps.get_section(s, key)
        old_title = (old.title if old else "") or ""
        old_content = (old.content if old else "") or ""
        ps.set_section(
            s,
            key,
            title=new_title,
            content=new_content,
            image_url=request.form.get("image_url", "").strip() or None,
            update_image_url=True,
        )
        s.commit()
        log_action("update_price_section", key)
        # New requirement: when a price line actually changes, every driver gets
        # a notification (default ON). The admin can opt out by ticking «Не
        # уведомлять водителей» (sends no_notify="1").
        changed_parts = []
        if new_title != old_title:
            changed_parts.append(f"Название: {new_title or '—'}")
        if new_content != old_content:
            changed_lines = ps.changed_content_lines(old_content, new_content)
            changed_parts.extend(changed_lines or ["Текст удалён"])
        changed = bool(changed_parts)
        notify_off = request.form.get("no_notify") == "1"
        if changed and not notify_off:
            section_name = new_title or ps.title_for(key)
            text = f"🏷 Изменение прайса — «{section_name}»\n" + "\n".join(changed_parts)
            broadcast.start_broadcast(text, "driver")
            flash(
                f"Раздел «{ps.title_for(key)}» сохранён. Уведомление отправляется водителям…",
                "success",
            )
        else:
            flash(f"Раздел «{ps.title_for(key)}» сохранён", "success")
        return redirect(url_for("price_sections_view"))

    root = ps.get_section(s, ps.ROOT_KEY)
    children = [ps.get_section(s, k) for k in ps.children_keys()]
    return render_template("price_sections.html", root=root, children=children)


@app.route("/price-sections/<string:section_key>/clear-photo", methods=["POST"])
@login_required
def clear_price_section_photo(section_key):
    s = db()
    if section_key not in ps.all_keys():
        abort(404)
    ps.set_section(s, section_key, file_id=None, update_file=True)
    s.commit()
    log_action("clear_price_section_photo", section_key)
    flash("Фото удалено", "success")
    return redirect(url_for("price_sections_view"))


# --------------------------------------------------------------------------- #
#  Admin logs                                                                  #
# --------------------------------------------------------------------------- #
@app.route("/logs")
@login_required
def logs():
    s = db()
    entries = s.query(AdminLog).order_by(AdminLog.created_at.desc()).limit(300).all()
    return render_template("logs.html", logs=entries)


# --------------------------------------------------------------------------- #
#  Excel export                                                                #
# --------------------------------------------------------------------------- #
@app.route("/export/stats.xlsx")
@login_required
def export_stats():
    buffer = build_stats_workbook(db())
    log_action("export_stats", "")
    filename = f"stats_{time_utils.now().date().isoformat()}.xlsx"
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.template_filter("dt")
def _fmt_dt(value) -> str:
    if not value:
        return ""
    return time_utils.format_local(value)


def _review_word(n: int) -> str:
    """Russian pluralization for 'отзыв' (mirrors bot/roles.py)."""
    n = abs(int(n))
    if 10 <= n % 100 <= 20:
        return "отзывов"
    last = n % 10
    if last == 1:
        return "отзыв"
    if 2 <= last <= 4:
        return "отзыва"
    return "отзывов"


ROLE_LABELS = {"passenger": "Пассажир", "driver": "Водитель", "dispatcher": "Диспетчер", "admin": "Администратор"}
ORDER_STATUS_LABELS = {"created": "Новая", "queued": "В очереди", "searching": "Ожидает водителя", "chat_search": "Поиск в чате", "assigned": "Водитель назначен", "arrived": "Водитель прибыл", "in_progress": "В пути", "completed": "Выполнен", "cancelled": "Отменён", "no_drivers": "Нет водителей"}
BOOKING_STATUS_LABELS = {
    "pending": "Бронь ожидает водителя",
    "assigned": "Водитель назначен",
    "driver_en_route": "Водитель выехал",
    "completed": "Выполнена",
    "canceled": "Отменена",
}
BOOKING_TYPE_LABELS = {
    "far_distance": "Межгород",
    "early_time": "К определённому времени",
}


@app.context_processor
def _ui_labels():
    return {
        "role_labels": ROLE_LABELS,
        "order_status_labels": ORDER_STATUS_LABELS,
        "booking_status_labels": BOOKING_STATUS_LABELS,
        "booking_type_labels": BOOKING_TYPE_LABELS,
    }


@app.template_filter("rating")
def _fmt_rating(user) -> str:
    """Render a driver rating as '⭐ 4.5 (12 отзывов)'. Accepts a User object."""
    count = getattr(user, "rating_count", 0) or 0
    if not count:
        return "⭐ — (нет отзывов)"
    avg = getattr(user, "rating", 0) or 0
    return f"⭐ {avg:.1f} ({count} {_review_word(count)})"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
