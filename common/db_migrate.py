"""Programmatic Alembic upgrade.

Running ``alembic upgrade head`` automatically on process startup keeps the
database schema in sync with ``common/models.py`` on every deploy, so adding a
new column/model never requires a manual migration step on the server
(bothost.ru / Docker / VPS). It is safe and idempotent: Alembic only applies
revisions that have not been recorded yet, and each migration additionally
guards its own column additions.
"""
from __future__ import annotations

import os
import threading
import logging
from contextlib import contextmanager

from common.logger import get_logger

log = get_logger("common.db_migrate")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INI = os.path.join(_PROJECT_ROOT, "alembic.ini")
_SCRIPTS = os.path.join(_PROJECT_ROOT, "migrations")

_lock = threading.Lock()
_done = False
_POSTGRES_MIGRATION_LOCK_ID = 846_382_163


@contextmanager
def _database_migration_lock():
    """Serialize Alembic across bot, admin and migration containers.

    ``threading.Lock`` protects only one Python process.  PostgreSQL advisory
    locking also protects deployments where several application processes
    start at the same time.  Non-PostgreSQL development databases keep the
    previous behaviour.
    """
    from sqlalchemy import text
    from common.database import engine

    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
        )
        log.info("Acquired PostgreSQL migration lock")
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
            )
            log.info("Released PostgreSQL migration lock")


def _remove_legacy_telegram_revision() -> None:
    """Replace the obsolete DB marker before Alembic loads its revision graph.

    The Telegram fork is not part of this application. Production databases
    that were stamped by that fork are moved back to the last shared VK
    revision; normal guarded migrations then apply the current VK schema.
    """
    try:
        from sqlalchemy import text
        from common.database import engine

        with engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE alembic_version "
                    "SET version_num=:vk_revision "
                    "WHERE version_num=:obsolete_revision"
                ),
                {
                    "vk_revision": "0034_temporary_driver_until",
                    "obsolete_revision": "0035_telegram_bridge",
                },
            )
        if result.rowcount:
            log.warning(
                "Removed obsolete migration marker; database will continue on the VK-only chain"
            )
    except Exception as exc:  # table may not exist on a fresh database
        log.debug("Legacy migration marker check skipped: %s", exc)


def ensure_schema() -> bool:
    """Apply any pending migrations. Returns True on success.

    Never raises: a migration failure is logged so the caller (bot long-poll
    loop / web app) can decide whether to keep running.
    """
    global _done
    with _lock:
        if _done:
            return True
        # IMPORTANT: migrations/base tables must be created BEFORE ALTER TABLE
        # guards. On a completely empty database the previous order made every
        # ALTER fail, created only a few standalone reliability tables, and
        # returned before Alembic could create users/orders/cities.
        try:
            if os.path.exists(_INI):
                from alembic import command
                from alembic.config import Config

                cfg = Config(_INI)
                # Use absolute paths so it works regardless of the current working
                # directory (systemd, Docker WORKDIR, `python -m bot.main`, etc.).
                cfg.set_main_option("script_location", _SCRIPTS)
                with _database_migration_lock():
                    _remove_legacy_telegram_revision()
                    command.upgrade(cfg, "head")
                # Alembic's fileConfig changes the root level to WARN. Restore
                # the application level so BotHost exposes startup failures.
                from common.config import config as app_config
                logging.getLogger().setLevel(app_config.LOG_LEVEL)
                for logger_name in ("bot.main", "bot.vk", "bot.outbox", "bot.pqueue", "common.db_migrate"):
                    logging.getLogger(logger_name).disabled = False
            else:
                # Hosting fallback when alembic.ini was not included: create
                # the complete current model, not just standalone guard tables.
                from common.database import engine
                from common.models import Base

                Base.metadata.create_all(bind=engine)
                log.warning("alembic.ini is absent; Base metadata created")

            if not ensure_columns():
                log.error("Required raw schema guard failed after base migration")
                return False
            _done = True
            log.info("Database base and schema guards completed")
            print("STARTUP DATABASE OK: schema is ready", flush=True)
            return True
        except Exception:  # noqa: BLE001 - caller decides whether to stop
            # Include the original DB/Alembic traceback. Previously only the
            # outer RuntimeError was visible in some hosting logs, hiding the
            # actual migration failure.
            log.exception("Auto-migration (alembic upgrade head) failed")
            return False


def ensure_columns() -> bool:
    """Idempotent raw DDL guard so required columns always exist, even where
    Alembic/migrations are unavailable (Docker image at /app, serverless).
    Safe to run on every startup; never raises."""
    # These data repairs are useful but must not block schema startup. Legacy
    # databases can contain historical duplicate VK rows; PostgreSQL may then
    # raise ix_users_vk_id UniqueViolation while rewriting unrelated rating
    # columns. Run the repairs best-effort and keep structural guards strict.
    rating_repair_statements = [
        "UPDATE users u SET rating_sum=COALESCE((SELECT SUM(r.stars) FROM reviews r WHERE r.driver_id=u.id AND r.kind='passenger_to_driver'), 0), rating_count=COALESCE((SELECT COUNT(r.id) FROM reviews r WHERE r.driver_id=u.id AND r.kind='passenger_to_driver'), 0)",
        "UPDATE users u SET passenger_rating_sum=COALESCE((SELECT SUM(r.stars) FROM reviews r WHERE r.passenger_id=u.id AND r.kind='driver_to_passenger'), 0), passenger_rating_count=COALESCE((SELECT COUNT(r.id) FROM reviews r WHERE r.passenger_id=u.id AND r.kind='driver_to_passenger'), 0)",
    ]
    push_statements = [
        "CREATE TABLE IF NOT EXISTS driver_push_devices (id SERIAL PRIMARY KEY, driver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, token TEXT NOT NULL UNIQUE, platform VARCHAR(20) NOT NULL DEFAULT 'android', is_active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "ALTER TABLE driver_push_devices ADD COLUMN IF NOT EXISTS device_secret_hash VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_driver_push_devices_driver_id ON driver_push_devices(driver_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_driver_push_devices_device_secret_hash ON driver_push_devices(device_secret_hash)",
        "CREATE TABLE IF NOT EXISTS push_scenarios (id SERIAL PRIMARY KEY, event_key VARCHAR(40) NOT NULL UNIQUE, title VARCHAR(120) NOT NULL, sound_file VARCHAR(255), duration_seconds INTEGER NOT NULL DEFAULT 60, repeat_seconds INTEGER NOT NULL DEFAULT 5, vibrate BOOLEAN NOT NULL DEFAULT TRUE, enabled BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS driver_app_pair_codes (id SERIAL PRIMARY KEY, driver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, code_hash VARCHAR(64) NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "ALTER TABLE driver_app_pair_codes ADD COLUMN IF NOT EXISTS is_reusable BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE driver_app_pair_codes ADD COLUMN IF NOT EXISTS label VARCHAR(120)",
        "CREATE INDEX IF NOT EXISTS ix_driver_app_pair_codes_driver_id ON driver_app_pair_codes(driver_id)",
        "CREATE TABLE IF NOT EXISTS rotating_chat_posts (id SERIAL PRIMARY KEY, text TEXT NOT NULL DEFAULT '', attachments TEXT, position INTEGER NOT NULL DEFAULT 0, is_active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE INDEX IF NOT EXISTS ix_rotating_chat_posts_position ON rotating_chat_posts(position, id)",
        "CREATE INDEX IF NOT EXISTS ix_rotating_chat_posts_active ON rotating_chat_posts(is_active)",
    ]
    statements = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS passenger_rating_sum INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS passenger_rating_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'passenger_to_driver'",
        "ALTER TABLE drivers_queue ADD COLUMN IF NOT EXISTS front_notified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE drivers_queue ADD COLUMN IF NOT EXISTS front_notice_outbox_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS driver_missed_offers INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS parallel_notice_outbox_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS parallel_notice_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE blocked_users ADD COLUMN IF NOT EXISTS notice_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE fake_calls ADD COLUMN IF NOT EXISTS payment_requested_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_phone VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_bank TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS show_payment_details BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR(20)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS route_text TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS voice_attachment TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ride_waiting_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_details_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS agreed_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_proposed_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_offer_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_offer_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS multi_car_group_id INTEGER",
        "CREATE SEQUENCE IF NOT EXISTS order_public_number_seq START WITH 1",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS public_number INTEGER",
        "ALTER TABLE orders ALTER COLUMN public_number SET DEFAULT nextval('order_public_number_seq')",
        "UPDATE orders SET public_number=id WHERE public_number IS NULL",
        "SELECT setval('order_public_number_seq', COALESCE(MAX(public_number), 1), MAX(public_number) IS NOT NULL) FROM orders",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_public_number ON orders(public_number)",
        "CREATE INDEX IF NOT EXISTS ix_orders_price_offer_driver_id ON orders(price_offer_driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_multi_car_group_id ON orders(multi_car_group_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_driver_was_offline BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_eta INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_eta_set_at TIMESTAMPTZ",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_notified_driver_ids TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_offer_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_offer_trip_id INTEGER REFERENCES orders(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_offer_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_auto_excluded_driver_ids TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_route_fallback BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS parallel_auto_offers_disabled BOOLEAN NOT NULL DEFAULT FALSE",
        # A reserved parallel request becomes the current ride only inside
        # promote_after_current. Older releases filled both ownership fields
        # too early, which could make completion/cancellation target the wrong
        # request.
        "UPDATE orders SET driver_id=NULL WHERE status='parallel_assigned' AND parallel_driver_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_orders_parallel_driver_id ON orders(parallel_driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_parallel_offer_driver_id ON orders(parallel_offer_driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_parallel_offer_trip_id ON orders(parallel_offer_trip_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offered_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_driver_id INTEGER REFERENCES users(id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_requested_driver_id ON orders(requested_driver_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offer_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS departure_prompt_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS passenger_rating_prompt_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_rating_prompt_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_actuality_prompt_outbox_id INTEGER",
        "ALTER TABLE passenger_queue ADD COLUMN IF NOT EXISTS actuality_prompt_outbox_id INTEGER",
        "UPDATE settings SET value='60' WHERE key='passenger_poll_timeout'",
        "UPDATE bot_messages SET text='Пожалуйста, оцените поездку:' WHERE key='ride_finished' AND text LIKE '%К оплате%'",
        "UPDATE settings SET value='2700' WHERE key IN ('driver_chat_timeout', 'driver_chat_far_timeout', 'driver_chat_delivery_timeout', 'booking_chat_timeout') AND value <> '2700'",
        "UPDATE settings SET value='🚗 Водитель готов выполнить вашу доставку за {price:.0f} ₽ + оплата чека из магазина. Согласны?' WHERE key='msg_delivery_offer' AND value LIKE '%foume%'",
        "UPDATE settings SET value='📅 Бронь дальние поездки' WHERE key='btn_booking' AND value IN ('📅 Забронировать поездку', 'Забронировать поездку', '📅 Бронь', 'Бронь')",
        "INSERT INTO cities (name, is_active) SELECT 'Чусовой', TRUE WHERE NOT EXISTS (SELECT 1 FROM cities WHERE LOWER(name)=LOWER('Чусовой'))",
        "INSERT INTO cities (name, is_active) SELECT 'Сараны', TRUE WHERE NOT EXISTS (SELECT 1 FROM cities WHERE LOWER(name)=LOWER('Сараны'))",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS actuality_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_notice_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS offer_notice_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS search_notice_outbox_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_offered_driver_id ON orders(offered_driver_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS declined_driver_ids TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS decline_reasons_json TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_decline_reason VARCHAR(40)",
        "UPDATE orders SET parallel_route_fallback=TRUE WHERE last_decline_reason='route_parallel_fallback' AND parallel_route_fallback=FALSE",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(64)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_departed_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_type VARCHAR(20)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_card VARCHAR(32)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_recipient VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_rules_sent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS driver_gender VARCHAR(10)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS away_notice_outbox_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS away_notice_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS temporary_driver_starts_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS temporary_driver_until TIMESTAMPTZ",
        """CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            passenger_id INTEGER NOT NULL REFERENCES users(id),
            driver_id INTEGER REFERENCES users(id),
            order_id INTEGER UNIQUE REFERENCES orders(id),
            type VARCHAR(30) NOT NULL,
            scheduled_time TIME NOT NULL,
            scheduled_at TIMESTAMPTZ NOT NULL,
            from_address VARCHAR(500) NOT NULL,
            to_address VARCHAR(500) NOT NULL DEFAULT '',
            route_text TEXT NOT NULL,
            extra_services TEXT,
            comment TEXT NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            canceled_by VARCHAR(20),
            reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_bookings_passenger_id ON bookings(passenger_id)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_driver_id ON bookings(driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_scheduled_at ON bookings(scheduled_at)",
        """CREATE TABLE IF NOT EXISTS driver_selections (
            id SERIAL PRIMARY KEY,
            passenger_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            driver_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_passenger_id ON driver_selections(passenger_id)",
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_driver_id ON driver_selections(driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_expires_at ON driver_selections(expires_at)",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS chat_notice_outbox_id INTEGER",
        "CREATE SEQUENCE IF NOT EXISTS booking_public_number_seq START WITH 1",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS public_number INTEGER",
        "ALTER TABLE bookings ALTER COLUMN public_number SET DEFAULT nextval('booking_public_number_seq')",
        "UPDATE bookings SET public_number=id WHERE public_number IS NULL",
        "SELECT setval('booking_public_number_seq', COALESCE(MAX(public_number), 1), MAX(public_number) IS NOT NULL) FROM bookings",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_public_number ON bookings(public_number)",
        "CREATE INDEX IF NOT EXISTS ix_orders_passenger_status_created ON orders(passenger_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_orders_driver_status_created ON orders(driver_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_orders_dispatcher_status_created ON orders(dispatcher_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_driver_queue_status_position ON drivers_queue(status, position)",
        "CREATE INDEX IF NOT EXISTS ix_passenger_queue_status_position ON passenger_queue(status, position)",
        "CREATE INDEX IF NOT EXISTS ix_bookings_status_scheduled ON bookings(status, scheduled_at)",
        """CREATE TABLE IF NOT EXISTS outbox_messages (
            id SERIAL PRIMARY KEY, peer_id BIGINT NOT NULL, text TEXT,
            keyboard TEXT, attachment TEXT,
            dont_parse_links BOOLEAN NOT NULL DEFAULT FALSE,
            random_id BIGINT NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ, sent_at TIMESTAMPTZ, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS processed_events (
            id SERIAL PRIMARY KEY, event_key VARCHAR(255) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id SERIAL PRIMARY KEY, job_key VARCHAR(120) NOT NULL UNIQUE,
            kind VARCHAR(40) NOT NULL, object_id INTEGER NOT NULL,
            run_at TIMESTAMPTZ NOT NULL, payload TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS login_attempts (
            id SERIAL PRIMARY KEY, ip_address VARCHAR(64) NOT NULL,
            login VARCHAR(120) NOT NULL, success BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_messages(status, next_attempt_at)",
        "ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS dont_parse_links BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_outbox_priority_pending ON outbox_messages(status, priority DESC, next_attempt_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_due ON scheduled_jobs(status, run_at)",
        "CREATE INDEX IF NOT EXISTS ix_login_attempts_lookup ON login_attempts(ip_address, login, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_processed_events_created ON processed_events(created_at)",
        # Release 0141 removed the aggregate unclaimed-order chat notice.
        # Cancel every legacy queued/retrying row before the outbox workers
        # start, otherwise an old retry could still post after deployment.
        """UPDATE outbox_messages
            SET status = 'cancelled', claimed_at = NULL,
                last_error = 'removed aggregate notice (0141)'
            WHERE text LIKE '🚕 Есть %'
              AND status NOT IN ('cancelled')""",
        """DELETE FROM settings WHERE key IN (
            'unclaimed_notice_outbox_id', 'unclaimed_notice_count',
            'unclaimed_notice_peer_id', 'unclaimed_notice_cleanup_version'
        )""",
        "UPDATE settings SET value = '👥 Водители' WHERE key = 'btn_drivers' AND value IN ('👥 Свободные водители', 'Свободные водители', 'Водители')",
    ]
    try:
        from sqlalchemy import text
        from common.database import engine
    except Exception as exc:  # noqa: BLE001
        log.error("ensure_columns: cannot import engine: %s", exc)
        return False
    # Skip the full-table repair when duplicate VK identifiers are visible.
    # Even if a race or legacy index state still raises, this non-structural
    # maintenance must never make the migrate service exit with code 1.
    try:
        with engine.begin() as conn:
            has_duplicate_vk_ids = bool(conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM users GROUP BY vk_id HAVING COUNT(*) > 1)"
            )).scalar())
        if has_duplicate_vk_ids:
            log.warning("Rating aggregate repair skipped: duplicate users.vk_id rows exist")
        else:
            for stmt in rating_repair_statements:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(stmt))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Optional rating aggregate repair skipped [%s]: %s", stmt, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Optional rating aggregate repair precheck skipped: %s", exc)

    ok = True
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.error("ensure_columns DDL failed [%s]: %s", stmt, exc)
    if ok:
        log.info("ensure_columns: schema columns verified")
    return ok
