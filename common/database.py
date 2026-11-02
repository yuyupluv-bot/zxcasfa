"""SQLAlchemy engine / session factory shared by the bot and the web admin.

Uses a synchronous engine (simpler + reliable on both bothost.ru and Vercel).
A scoped/context-managed session helper is provided for safe usage.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import config

def _connection_args() -> dict:
    """Bound connection time; SQL limits are enabled only where configured."""
    if not config.sqlalchemy_url().startswith("postgresql"):
        return {}
    options = []
    for setting, value in (
        ("statement_timeout", config.DB_STATEMENT_TIMEOUT_MS),
        ("lock_timeout", config.DB_LOCK_TIMEOUT_MS),
        ("idle_in_transaction_session_timeout", config.DB_IDLE_TRANSACTION_TIMEOUT_MS),
    ):
        if value:
            options.append(f"-c {setting}={int(value)}")
    args = {"connect_timeout": config.DB_CONNECT_TIMEOUT}
    if options:
        args["options"] = " ".join(options)
    return args


# pool_pre_ping avoids stale connections (important for Supabase/Neon which
# drop idle connections). pool_recycle keeps connections under the provider
# idle timeout.
engine = create_engine(
    config.sqlalchemy_url(),
    connect_args=_connection_args(),
    pool_pre_ping=True,
    pool_size=config.DB_POOL_SIZE,
    max_overflow=config.DB_MAX_OVERFLOW,
    pool_recycle=config.DB_POOL_RECYCLE,
    pool_timeout=config.DB_POOL_TIMEOUT,
    pool_use_lifo=True,
    future=True,
)

@event.listens_for(engine, "connect")
def _set_yekaterinburg_timezone(dbapi_connection, _connection_record):
    """Every PostgreSQL session reads/writes wall time as Asia/Yekaterinburg."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET TIME ZONE 'Asia/Yekaterinburg'")
    finally:
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
_active_session: ContextVar[Session | None] = ContextVar("active_db_session", default=None)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    token = _active_session.set(session)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        _active_session.reset(token)
        session.close()


def get_session() -> Session:
    """Return a raw session (caller is responsible for commit/close)."""
    return SessionLocal()


def current_session() -> Session | None:
    """Session participating in the current bot event transaction, if any."""
    return _active_session.get()
