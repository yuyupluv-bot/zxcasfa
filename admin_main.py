"""Production Gunicorn launcher; no Flask/DB import in the parent process."""
from __future__ import annotations
import os
import sys
from common.admin_supervisor import positive_env, run_supervised


def gunicorn_command() -> list[str]:
    port = positive_env('PORT', 3000)
    return [
        sys.executable, '-m', 'gunicorn', 'wsgi:app',
        '--bind', f'0.0.0.0:{port}',
        '--workers', str(positive_env('WEB_WORKERS', 1)),
        '--threads', str(positive_env('WEB_THREADS', 4)),
        '--worker-class', 'gthread',
        '--timeout', str(positive_env('WEB_TIMEOUT', 60)),
        '--graceful-timeout', str(positive_env('WEB_GRACEFUL_TIMEOUT', 30)),
        '--max-requests', str(positive_env('WEB_MAX_REQUESTS', 2000)),
        '--max-requests-jitter', str(positive_env('WEB_MAX_REQUESTS_JITTER', 200, minimum=0)),
        '--access-logfile', '-', '--error-logfile', '-',
    ]


if __name__ == '__main__':
    command = gunicorn_command()
    if os.getenv('ADMIN_WATCHDOG_ENABLED', '1').strip().lower() in ('0', 'false', 'no'):
        os.execvp(sys.executable, command)
    raise SystemExit(run_supervised(command, positive_env('PORT', 3000)))
