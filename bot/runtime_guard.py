"""Process-local hang detector. Recovery relies on Docker restart: unless-stopped.

No periodic reboot: a quiet bot is healthy. Track completed Long Poll requests,
outbox coordinator progress, and the age of RUNNING event/timer/outbox tasks.
Before a forced exit, dump every Python thread to the container log.
"""
from __future__ import annotations

import faulthandler
import itertools
import logging
import os
import sys
import threading
import time

log = logging.getLogger("bot.runtime_guard")
_lock = threading.Lock()
_started = False
_startup_at: float | None = None
_ready = False
_pulses: dict[str, float] = {}
_active: dict[int, tuple[str, str, float]] = {}
_ids = itertools.count(1)


def seconds(name: str, default: int, minimum: int = 30) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (ValueError, TypeError):
        return default


def start() -> None:
    global _started, _startup_at
    if os.getenv("BOT_WATCHDOG_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    with _lock:
        if _started:
            return
        _started = True
        _startup_at = time.monotonic()
    threading.Thread(target=_monitor, name="bot-watchdog", daemon=True).start()


def ready() -> None:
    global _ready
    with _lock:
        _ready = True
        _pulses.setdefault("longpoll", time.monotonic())
        _pulses.setdefault("outbox-loop", time.monotonic())
    log.info("Bot watchdog ready; monitors request/task progress, not user traffic")


def pulse(name: str) -> None:
    with _lock:
        _pulses[name] = time.monotonic()


def call(kind: str, label: str, callback, *args, **kwargs):
    token = next(_ids)
    with _lock:
        _active[token] = (kind, label, time.monotonic())
    try:
        return callback(*args, **kwargs)
    finally:
        with _lock:
            _active.pop(token, None)


def stalled_reason(now: float | None = None) -> str | None:
    now = time.monotonic() if now is None else now
    with _lock:
        if _startup_at is not None and not _ready:
            if now - _startup_at > seconds("BOT_STARTUP_TIMEOUT", 180):
                return "startup exceeded deadline"
            return None
        for name, stamp in _pulses.items():
            env = "BOT_POLL_STALL_TIMEOUT" if name == "longpoll" else "BOT_OUTBOX_STALL_TIMEOUT"
            if now - stamp > seconds(env, 90):
                return f"{name} made no progress for {int(now - stamp)}s"
        limit = seconds("BOT_TASK_TIMEOUT", 180)
        for kind, label, stamp in _active.values():
            if now - stamp > limit:
                return f"running {kind} task {label} exceeded {limit}s (age={int(now - stamp)}s)"
    return None


def _monitor() -> None:
    while True:
        time.sleep(5)
        reason = stalled_reason()
        if reason is None:
            continue
        log.error("Possible bot stall: %s; collecting thread stacks", reason)
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception:
            log.exception("Could not dump Python thread stacks")
        # Require a second failed check. A task finishing now cancels recovery.
        time.sleep(10)
        reason = stalled_reason()
        if reason is None:
            log.warning("Bot made progress; watchdog recovery cancelled")
            continue
        log.critical("Exiting stuck bot (code=70): %s; Docker restart policy must restart this container", reason)
        try:
            sys.stderr.flush()
            sys.stdout.flush()
        finally:
            # sys.exit in a thread does not kill stuck non-daemon workers.
            # On process exit PostgreSQL rolls back open transactions.
            os._exit(70)
