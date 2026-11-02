"""Bounded liveness watchdog for this admin's Gunicorn process only.

No Docker socket, no reboot timer, and no database credentials are needed.
A responsive /live is independent of PostgreSQL readiness (/health).
"""
from __future__ import annotations
import http.client
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def positive_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def supervisor_logger() -> logging.Logger:
    log = logging.getLogger('admin.supervisor')
    log.setLevel(logging.INFO)
    if not log.handlers:
        formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        log.addHandler(stream)
        try:
            folder = Path(os.getenv('LOG_DIR', '/app/logs'))
            folder.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(folder / 'admin-supervisor.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(formatter)
            log.addHandler(handler)
        except OSError as error:
            log.warning('Persistent supervisor log unavailable: %s', error)
    return log


def probe_liveness(port: int, timeout: int) -> tuple[bool, str]:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        connection.request('GET', '/live')
        response = connection.getresponse()
        response.read(1024)
        return response.status == 200, f'HTTP {response.status}'
    except (OSError, http.client.HTTPException) as error:
        return False, f'{type(error).__name__}: {error}'
    finally:
        connection.close()


def stop_server(process, log: logging.Logger, grace: int = 30) -> None:
    if process.poll() is not None:
        return
    try:
        # Every supervised child starts its own process group. Never signal
        # the bot, PostgreSQL, proxy, or another container's process.
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.error('Gunicorn did not stop gracefully; terminating its process group pid=%s', process.pid)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait(timeout=5)


def run_supervised(command: list[str], port: int) -> int:
    log = supervisor_logger()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    interval = positive_env('ADMIN_WATCHDOG_INTERVAL', 15)
    timeout = positive_env('ADMIN_WATCHDOG_TIMEOUT', 3)
    threshold = positive_env('ADMIN_WATCHDOG_FAILURES', 4, minimum=2)
    startup_grace = positive_env('ADMIN_WATCHDOG_STARTUP_GRACE', 120)
    failures = 0
    process = subprocess.Popen(command, start_new_session=True)
    started = time.monotonic()
    log.info('Gunicorn started pid=%s port=%s; liveness watchdog active', process.pid, port)
    try:
        while not stop.wait(interval):
            code = process.poll()
            if code is not None:
                log.error('Gunicorn exited unexpectedly code=%s; container restart policy will handle recovery', code)
                return code if code else 1
            if time.monotonic() - started < startup_grace:
                continue
            healthy, detail = probe_liveness(port, timeout)
            if healthy:
                if failures:
                    log.info('Liveness restored after %s failed probes', failures)
                failures = 0
                continue
            failures += 1
            log.warning('Admin liveness failure %s/%s: %s', failures, threshold, detail)
            if failures < threshold:
                continue
            log.error('Restarting unresponsive Gunicorn pid=%s after %s consecutive liveness failures', process.pid, failures)
            stop_server(process, log)
            if stop.wait(5):
                break
            process = subprocess.Popen(command, start_new_session=True)
            started = time.monotonic()
            failures = 0
            log.info('Gunicorn restarted pid=%s', process.pid)
        return 0
    finally:
        stop_server(process, log)
