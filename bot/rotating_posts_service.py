"""Persistent round-robin publisher for a dedicated VK community chat."""
from __future__ import annotations

import datetime as dt
import threading
import time

from sqlalchemy import text as sql_text

from common import time_utils
from common.database import session_scope
from common.logger import get_logger
from common.models import OutboxMessage, RotatingChatPost
from common.settings_service import get_int, get_setting, set_setting

from .vk_client import vk

log = get_logger("bot.rotating_posts")

_started = False
_start_lock = threading.Lock()
_ADVISORY_LOCK_ID = 1_579_050
_POLL_SECONDS = 15


def _enabled(session) -> bool:
    return str(get_setting(session, "rotating_posts_enabled", "0")).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _parse_last_sent(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _claim_cycle(session) -> bool:
    """Prevent duplicate posts if two bot processes overlap during deploy."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        session.execute(
            sql_text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": _ADVISORY_LOCK_ID},
        ).scalar()
    )


def publish_due_once() -> bool:
    """Publish exactly one due post and persist the next round-robin position."""
    with session_scope() as session:
        if not _claim_cycle(session) or not _enabled(session):
            return False

        peer_id = get_int(session, "rotating_posts_peer_id", 0)
        interval = max(1, get_int(session, "rotating_posts_interval_minutes", 15))
        if peer_id <= 2_000_000_000:
            return False

        now = time_utils.now()
        last_sent = _parse_last_sent(
            get_setting(session, "rotating_posts_last_sent_at", "")
        )
        if last_sent is None:
            # Enabling starts a full interval; it must not publish immediately
            # while an administrator is still adding the first posts.
            set_setting(session, "rotating_posts_last_sent_at", now.isoformat())
            return False
        if now - last_sent < dt.timedelta(minutes=interval):
            return False

        posts = (
            session.query(RotatingChatPost)
            .filter(RotatingChatPost.is_active.is_(True))
            .order_by(RotatingChatPost.position.asc(), RotatingChatPost.id.asc())
            .all()
        )
        if not posts:
            return False

        available, details = vk.check_community_chat_access(peer_id)
        if not available:
            # Throttle a broken peer_id to one check per configured interval
            # and never create an outbox backlog that cannot be delivered.
            set_setting(session, "rotating_posts_last_sent_at", now.isoformat())
            log.error(
                "Rotating-post chat is unavailable: peer=%s error=%s",
                peer_id,
                details,
            )
            return False

        last_id = get_int(session, "rotating_posts_last_post_id", 0)
        post = posts[0]
        for index, candidate in enumerate(posts):
            if candidate.id == last_id:
                post = posts[(index + 1) % len(posts)]
                break

        queued = vk.send_message(
            peer_id,
            post.text or "",
            attachment=post.attachments or None,
            dont_parse_links=True,
        )
        if not queued:
            log.error(
                "Rotating post was not queued: post=%s peer=%s",
                post.id,
                peer_id,
            )
            return False

        set_setting(session, "rotating_posts_last_post_id", str(post.id))
        set_setting(session, "rotating_posts_last_sent_at", now.isoformat())
        log.info(
            "Rotating post queued: post=%s peer=%s interval=%s",
            post.id,
            peer_id,
            interval,
        )
        return True


def cancel_stale_backlog() -> int:
    """Cancel old failed attempts before enabling the corrected token route.

    The dedicated peer is used only for rotating posts. Without this startup
    cleanup, every failed v158 row could be delivered in a burst immediately
    after the token-selection fix is deployed.
    """
    with session_scope() as session:
        peer_id = get_int(session, "rotating_posts_peer_id", 0)
        if peer_id <= 2_000_000_000:
            return 0
        cancelled = (
            session.query(OutboxMessage)
            .filter(
                OutboxMessage.peer_id == peer_id,
                OutboxMessage.status.in_(("pending", "failed", "sending")),
                OutboxMessage.priority == 0,
            )
            .update(
                {
                    OutboxMessage.status: "cancelled",
                    OutboxMessage.claimed_at: None,
                    OutboxMessage.last_error: "cancelled stale rotating-post backlog",
                },
                synchronize_session=False,
            )
        )
        if cancelled:
            log.warning(
                "Cancelled stale rotating-post backlog: peer=%s rows=%s",
                peer_id,
                cancelled,
            )
        return int(cancelled or 0)


def _worker() -> None:
    while True:
        time.sleep(_POLL_SECONDS)
        try:
            publish_due_once()
        except Exception as exc:  # noqa: BLE001
            log.exception("Rotating-post worker failed: %s", exc)


def start_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        threading.Thread(
            target=_worker,
            name="rotating-chat-posts",
            daemon=True,
        ).start()
        _started = True
        log.info("Rotating-post worker started")