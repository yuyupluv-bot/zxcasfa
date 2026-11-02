"""BotHost entrypoint: validate -> migrate once -> supervise bot and Gunicorn.

No VK/Flask imports in the parent; bot and web failures stay isolated.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('bothost')


def validate_environment(env: dict[str, str]) -> list[str]:
    errors = []
    required = ('DATABASE_URL', 'VK_TOKEN', 'VK_GROUP_ID', 'SECRET_KEY', 'ADMIN_LOGIN')
    for name in required:
        value = env.get(name, '').strip()
        if not value or value.startswith(('CHANGE_', 'PASTE_', 'REPLACE_')):
            errors.append(f'{name}: задайте значение в переменных окружения BotHost')
    if not (env.get('ADMIN_PASSWORD', '').strip() or env.get('ADMIN_PASSWORD_HASH', '').strip()):
        errors.append('ADMIN_PASSWORD или ADMIN_PASSWORD_HASH: задайте пароль админки')
    elif env.get('ADMIN_PASSWORD', '').startswith(('CHANGE_', 'PASTE_', 'REPLACE_')):
        errors.append('ADMIN_PASSWORD: замените пример настоящим паролем')
    if env.get('SECRET_KEY') and len(env['SECRET_KEY']) < 32:
        errors.append('SECRET_KEY: требуется минимум 32 символа')
    for name, default, maximum in (('VK_GROUP_ID', '0', None), ('PORT', '3000', 65535)):
        try:
            value = int(env.get(name, default))
            if value <= 0 or (maximum and value > maximum):
                raise ValueError
        except ValueError:
            errors.append(f'{name}: требуется положительное целое число' + (' не больше 65535' if maximum else ''))
    if env.get('DATABASE_URL'):
        try:
            url = urlsplit(env['DATABASE_URL'])
            if url.scheme not in ('postgres', 'postgresql', 'postgresql+psycopg2') or not url.hostname or not url.path.strip('/'):
                raise ValueError
        except ValueError:
            errors.append('DATABASE_URL: требуется URL PostgreSQL с хостом и именем базы (не SQLite)')
    return errors


def child_environment(role: str) -> dict[str, str]:
    env = os.environ.copy()
    env['AUTO_MIGRATE_ON_START'] = '0'
    env['PYTHONUNBUFFERED'] = '1'
    prefix = 'BOT' if role == 'bot' else 'ADMIN'
    for name, default in (('DB_STATEMENT_TIMEOUT_MS', '30000'), ('DB_LOCK_TIMEOUT_MS', '5000'),
                          ('DB_IDLE_TRANSACTION_TIMEOUT_MS', '60000')):
        env[name] = env.get(f'{prefix}_{name}', env.get(name, default))
    return env


def stop_process(process: subprocess.Popen, grace: float = 30) -> None:
    # Signal the entire group, including admin_main's supervised Gunicorn group.
    # admin_main forwards TERM to its own Gunicorn group in its finally block.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def supervise(stop: threading.Event, commands: dict[str, list[str]] | None = None) -> int:
    commands = commands or {
        'bot': [sys.executable, '-u', 'main.py'],
        'admin': [sys.executable, '-u', 'admin_main.py'],
    }
    children: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    due = dict.fromkeys(commands, 0.0)
    failures = dict.fromkeys(commands, 0)
    try:
        while not stop.is_set():
            now = time.monotonic()
            for role, command in commands.items():
                if stop.is_set():
                    break
                process = children.get(role)
                if process is not None and process.poll() is not None:
                    code = process.returncode
                    stop_process(process, grace=1)
                    del children[role]
                    failures[role] = 1 if now - started[role] >= 120 else failures[role] + 1
                    delay = min(60, 2 ** min(failures[role], 6))
                    due[role] = now + delay
                    log.error('%s exited code=%s; restart in %ss (other service stays running)', role, code, delay)
                if role not in children and now >= due[role]:
                    children[role] = subprocess.Popen(command, cwd=ROOT, env=child_environment(role), start_new_session=True)
                    started[role] = now
                    log.info('%s started pid=%s', role, children[role].pid)
            stop.wait(0.25)
        return 0
    finally:
        # Send TERM to both before waiting so shutdown fits the hosting deadline.
        for process in children.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in children.values():
            stop_process(process)


def main() -> int:
    os.chdir(ROOT)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    errors = validate_environment(dict(os.environ))
    if errors:
        for error in errors:
            log.error(error)  # Only setting names, never secret values.
        return 2
    os.environ.setdefault('PORT', '3000')
    os.environ.setdefault('FLASK_COOKIE_SECURE', '1')
    os.environ.setdefault('LOG_TO_FILE', '0')
    os.environ.setdefault('DB_POOL_SIZE', '5')
    os.environ.setdefault('DB_MAX_OVERFLOW', '5')
    os.environ.setdefault('BOT_WORKERS', '8')
    os.environ.setdefault('OUTBOX_WORKERS', '4')
    os.environ.setdefault('TIMER_WORKERS', '4')
    os.environ.setdefault('PUSH_SOUND_DIR', '/app/data/push_sounds')
    os.environ.setdefault('ADMIN_WATCHDOG_ENABLED', '1')
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    log.info('STARTUP BOTHOST: checking database and applying migrations before bot/admin')
    migration = subprocess.Popen([
        sys.executable, '-u', '-c',
        'from common.db_migrate import ensure_schema; raise SystemExit(0 if ensure_schema() else 1)',
    ], cwd=ROOT, start_new_session=True)
    deadline = time.monotonic() + 300
    try:
        while migration.poll() is None:
            if stop.wait(0.25) or time.monotonic() >= deadline:
                stop_process(migration)
                log.error('Migration interrupted or exceeded 300 seconds')
                return 0 if stop.is_set() else 1
        if migration.returncode:
            log.error('Migration failed; bot/admin NOT started. Check DATABASE_URL and migration logs.')
            return 1
        if stop.is_set():
            return 0
        log.info('STARTUP BOTHOST DATABASE OK; starting bot and admin on PORT=%s', os.environ['PORT'])
        return supervise(stop)
    finally:
        if migration.poll() is None:
            stop_process(migration)


if __name__ == '__main__':
    raise SystemExit(main())
