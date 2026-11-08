"""Bot entry point: VK Bot Long Poll loop.

Run as a persistent process on bothost.ru (screen/systemd):

    python -m bot.main

The long-poll loop is intentionally simple and never crashes on a single
event: every message is handled inside its own DB transaction, and errors are
logged. threading.Timer (see bot/timers.py) provides non-blocking timers for
the 3-minute waiting window and the driver accept timeout, so the poll loop is
never blocked.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from sqlalchemy.dialects.postgresql import insert as pg_insert

# Allow launching as a plain script (e.g. `python bot/main.py` on bothost.ru),
# not only as a module (`python -m bot.main`). Without this, the project root is
# not on sys.path and `import common...` fails with ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    from bot import runtime_guard
    runtime_guard.start()

from vk_api.bot_longpoll import VkBotEventType  # noqa: E402

from common.database import session_scope
from common.config import config
from common.db_migrate import ensure_schema
from common.logger import get_logger

# Absolute imports (not relative) so the file also works when bothost.ru
# launches it directly as `python bot/main.py` (where __package__ is empty and
# `from .handlers ...` would raise "attempted relative import with no known
# parent package"). The sys.path bootstrap above puts the project root on the
# path, so `bot` is importable as a package.
from bot.handlers import handle_group_join, handle_message
from bot import accept_timeout_service, away_order_notice_service, booking_service, driver_away_timeout_service, maintenance_service, order_service, outbox_service, parallel_orders, passenger_queue, rating_expiry_service, rotating_posts_service, temporary_driver_service
from bot.vk_client import vk

log = get_logger("bot.main")

# Sharded single-thread executors preserve event order for each user while
# processing different users concurrently. A bounded semaphore provides
# backpressure instead of allowing an unbounded in-memory task queue.
_event_executors = [
    ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"vk-events-{index}")
    for index in range(config.BOT_WORKERS)
]
_pending_events = threading.BoundedSemaphore(config.BOT_MAX_PENDING_EVENTS)


def _event_message(event) -> dict:
    """Normalize MESSAGE_NEW/MESSAGE_REPLY payloads from all vk_api shapes.

    vk_api exposes MESSAGE_NEW as obj.message, but MESSAGE_REPLY can expose the
    message directly as obj. Some versions also wrap it in obj.object.message.
    Never let an optional wrapper crash the Long Poll event worker.
    """
    obj = getattr(event, "obj", None)
    candidates = [getattr(obj, "message", None)]
    if hasattr(obj, "get"):
        candidates.extend((obj.get("message"), obj.get("object"), obj))
    nested = getattr(obj, "object", None)
    if nested is not None:
        candidates.extend((getattr(nested, "message", None), nested))
    for candidate in candidates:
        if candidate is None:
            continue
        if hasattr(candidate, "get"):
            nested_message = candidate.get("message")
            if nested_message is not None and hasattr(nested_message, "get"):
                candidate = nested_message
            if candidate.get("peer_id") is not None or candidate.get("conversation_message_id") is not None:
                return candidate
    return {}


def _is_community_outgoing(message: dict) -> bool:
    """VK can report chat sends as MESSAGE_NEW instead of MESSAGE_REPLY."""
    if not message:
        return False
    from_id = int(message.get("from_id") or 0)
    return bool(message.get("out")) or (
        from_id != 0 and abs(from_id) == abs(int(config.VK_GROUP_ID or 0))
    )


def process_event(event) -> None:
    if event.type == VkBotEventType.MESSAGE_NEW:
        message = _event_message(event)
        with session_scope() as session:
            if not _claim_event(session, event):
                return
            vk.observe_chat_message(message)
            if _is_community_outgoing(message):
                outbox_service.record_outgoing_message_event(session, message)
            else:
                from common.driver_action_lock import acquire
                acquire(session, int(message.get("from_id") or 0))
                handle_message(session, event)
    elif event.type == VkBotEventType.MESSAGE_REPLY:
        message = _event_message(event)
        if not message:
            log.error("MESSAGE_REPLY event has no usable message payload: %r", event.obj)
            return
        with session_scope() as session:
            if not _claim_event(session, event):
                return
            vk.observe_chat_message(message)
            outbox_service.record_outgoing_message_event(session, message or {})
    elif event.type == VkBotEventType.GROUP_JOIN:
        obj = event.obj
        vk_id = getattr(obj, "user_id", None)
        if vk_id is None and isinstance(obj, dict):
            vk_id = obj.get("user_id")
        with session_scope() as session:
            if not _claim_event(session, event):
                return
            handle_group_join(session, int(vk_id or 0))


def _event_key(event) -> str:
    explicit = getattr(event, "event_id", None)
    if explicit:
        return f"event:{explicit}"
    if event.type in (VkBotEventType.MESSAGE_NEW, VkBotEventType.MESSAGE_REPLY):
        message = _event_message(event)
        return "message:%s:%s" % (
            message.get("peer_id") or 0,
            message.get("conversation_message_id") or message.get("id") or repr(message),
        )
    return f"{event.type}:{_event_actor_id(event)}:{repr(event.obj)}"


def _claim_event(session, event) -> bool:
    from common.models import ProcessedEvent
    key = _event_key(event)[:255]
    if session.get_bind().dialect.name == "postgresql":
        statement = (
            pg_insert(ProcessedEvent)
            .values(event_key=key)
            .on_conflict_do_nothing(index_elements=[ProcessedEvent.event_key])
            .returning(ProcessedEvent.id)
        )
        return session.execute(statement).scalar_one_or_none() is not None
    if session.query(ProcessedEvent.id).filter(ProcessedEvent.event_key == key).first():
        return False
    session.add(ProcessedEvent(event_key=key))
    session.flush()
    return True


def _event_actor_id(event) -> int:
    try:
        if event.type in (VkBotEventType.MESSAGE_NEW, VkBotEventType.MESSAGE_REPLY):
            message = _event_message(event)
            peer_id = int(message.get("peer_id") or 0)
            # Serialize each chat so a reply cannot overtake the original.
            if peer_id >= 2_000_000_000:
                return peer_id
            return int(message.get("from_id") or peer_id or 0)
        obj = event.obj
        value = getattr(obj, "user_id", None)
        if value is None and isinstance(obj, dict):
            value = obj.get("user_id")
        return int(value or 0)
    except Exception:  # noqa: BLE001
        return 0


def _event_done(future: Future) -> None:
    _pending_events.release()
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to process queued event: %s", exc)


def _run_event(event) -> None:
    from bot import runtime_guard
    runtime_guard.call("event", str(_event_actor_id(event)), process_event, event)


def submit_event(event) -> None:
    """Queue an event with bounded backpressure and per-user ordering."""
    _pending_events.acquire()
    actor_id = _event_actor_id(event)
    executor = _event_executors[abs(actor_id) % len(_event_executors)]
    executor.submit(_run_event, event).add_done_callback(_event_done)


def run() -> None:
    from bot import runtime_guard
    runtime_guard.start()
    # Keep the DB schema in sync with the models on every startup.
    if os.getenv("AUTO_MIGRATE_ON_START", "1") != "0" and not ensure_schema():
        raise RuntimeError("Не удалось применить обязательные миграции базы данных")
    from common.logger import restore_bot_formatters
    restore_bot_formatters()
    print(
        f"STARTUP VK CHECK: group_id={config.VK_GROUP_ID}, token_present={bool(config.VK_TOKEN)}",
        flush=True,
    )
    try:
        vk_settings = vk.validate_startup()
    except Exception as exc:  # noqa: BLE001
        print(f"STARTUP VK ERROR: {type(exc).__name__}: {exc}", flush=True)
        raise
    print(
        "STARTUP VK OK: Long Poll and community messages are available; "
        f"api_version={vk_settings.get('api_version', config.VK_API_VERSION)}",
        flush=True,
    )
    from bot import timers
    timers.restore_persistent()
    recovered_rating_prompts = rating_expiry_service.schedule_missing_for_open_prompts()
    if recovered_rating_prompts:
        log.info("Scheduled missing rating expiries: %s", recovered_rating_prompts)
    accept_timeout_service.start_worker()
    repaired_route_offers = parallel_orders.reconcile_route_offers()
    if repaired_route_offers:
        log.warning("Reconciled route-parallel offers: %s", repaired_route_offers)
    driver_away_timeout_service.schedule_missing_for_away_drivers()
    rotating_posts_service.cancel_stale_backlog()
    outbox_service.start_worker()
    repaired_offers = order_service.reassign_duplicate_pending_offers()
    if repaired_offers:
        log.warning("Reassigned duplicate pending offers: %s", repaired_offers)
    away_order_notice_service.start_worker()
    parallel_orders.start_notice_worker()
    passenger_queue.start_worker()
    booking_service.start_reminder_worker()
    maintenance_service.start_worker()
    temporary_driver_service.start_worker()
    rotating_posts_service.start_worker()
    from bot import common_chat_notices
    common_chat_notices.start_worker()
    log.info("Taxi bot started. Listening for events…")
    print("STARTUP BOT OK: listening for VK events", flush=True)
    from bot import driver_app_commands
    driver_app_commands.start_worker()
    runtime_guard.ready()
    while True:
        try:
            # check() completes even when there are no messages. A quiet night
            # must not be mistaken for a stalled connection.
            runtime_guard.pulse("longpoll")
            for event in vk.longpoll.check():
                submit_event(event)
        except Exception as exc:  # noqa: BLE001 - reconnect on network errors
            log.error("Long poll error, reconnecting in 3s: %s", exc)
            print(f"LONG POLL ERROR: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    run()
