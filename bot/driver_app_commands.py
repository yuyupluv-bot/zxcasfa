"""Mobile commands are processed here, beside VK, with the ORIGINAL handlers.

All timer scheduling happens in the bot process. Flask only enqueues commands.
A command receipt and its business changes commit/rollback together.
"""
from __future__ import annotations
import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from common.database import session_scope
from common.models import DriverAppCommand, DriverPushDevice, User, Setting
from common import time_utils
from common.driver_app_state import project, canonical, iso
from common.driver_action_lock import acquire
from common.logger import get_logger
from . import runtime_guard
log=get_logger('bot.driver_app_commands')
_started=False


def execute(command_id):
    from .handlers import handle_driver
    from .states_service import get_state
    with session_scope() as session:
        command=session.query(DriverAppCommand).filter(DriverAppCommand.id==command_id).with_for_update().one_or_none()
        if not command or command.status!='running':return
        now=time_utils.now()
        driver=session.get(User,command.driver_id)
        device=session.get(DriverPushDevice,command.device_id)
        if not driver or not device or device.driver_id!=command.driver_id or not driver.has_role('driver') or driver.is_blocked or driver.role!='driver':
            command.status='rejected';command.error='Доступ водителя изменился.';command.finished_at=now;return
        acquire(session,driver.vk_id)
        session.refresh(driver)
        snapshot=project(session,driver)
        if dt.datetime.fromisoformat(iso(command.expires_at))<now or snapshot['revision']!=command.revision:
            command.status='rejected';command.error='Состояние изменилось. Обновите экран и повторите действие.';command.finished_at=now;return
        body=json.loads(command.body)
        payload=body.get('payload') or {}
        text=body.get('text') or ''
        if payload:
            allowed={canonical(a['payload']) for a in snapshot['actions']}
            if canonical(payload) not in allowed:
                command.status='rejected';command.error='Действие больше недоступно.';command.finished_at=now;return
        elif not snapshot['input_hint'] or not text.strip():
            command.status='rejected';command.error='Сейчас ввод текста не ожидается.';command.finished_at=now;return
        # The shared session_scope binds current_session, so all VK notices
        # enter the transactional outbox instead of synchronous network sends.
        handle_driver(session,driver,get_state(session,driver.vk_id).state,text,payload,[])
        command.status='done';command.finished_at=time_utils.now();command.error=None
        log.info('Mobile action executed command=%s driver=%s',command.id,driver.id)


def _run(command_id):
    try:runtime_guard.call('app-command',str(command_id),execute,command_id)
    except Exception:
        log.exception('Mobile action failed command=%s',command_id)
        with session_scope() as session:
            command=session.get(DriverAppCommand,command_id)
            if command and command.status=='running':
                command.status='failed';command.error='Действие не выполнено. Обновите экран перед повтором.'
                command.finished_at=time_utils.now()


def _heartbeat():
    with session_scope() as session:
        row=session.query(Setting).filter(Setting.key=='driver_app_worker_heartbeat').one_or_none()
        if row is None:row=Setting(key='driver_app_worker_heartbeat');session.add(row)
        row.value=time_utils.now().isoformat()


def _worker():
    executor=ThreadPoolExecutor(max_workers=4,thread_name_prefix='driver-app-command')
    active={};last_heartbeat=0.0;last_cleanup=0.0
    while True:
        runtime_guard.pulse('app-commands')
        try:
            for future in list(active):
                if future.done():active.pop(future);future.result()
            if time.monotonic()-last_heartbeat>5:
                _heartbeat();last_heartbeat=time.monotonic()
            if time.monotonic()-last_cleanup>3600:
                with session_scope() as session:
                    session.query(DriverAppCommand).filter(DriverAppCommand.created_at<time_utils.now()-dt.timedelta(days=7),DriverAppCommand.status.in_(('done','rejected','failed'))).delete(synchronize_session=False)
                last_cleanup=time.monotonic()
            if len(active)<4:
                with session_scope() as session:
                    rows=session.query(DriverAppCommand).filter(DriverAppCommand.status=='pending').order_by(DriverAppCommand.id).with_for_update(skip_locked=True).limit(4-len(active)).all()
                    ids=[]
                    for row in rows:row.status='running';ids.append(row.id)
                for id in ids:active[executor.submit(_run,id)]=id
            time.sleep(.2)
        except Exception:
            log.exception('Mobile command coordinator failed');time.sleep(1)


def start_worker():
    global _started
    if _started:return
    with session_scope() as session:
        session.query(DriverAppCommand).filter(DriverAppCommand.status=='running').update({'status':'pending'},synchronize_session=False)
        # Keep replay receipts for a week; no unlimited technical table growth.
        session.query(DriverAppCommand).filter(DriverAppCommand.created_at<time_utils.now()-dt.timedelta(days=7)).delete(synchronize_session=False)
    _started=True
    threading.Thread(target=_worker,name='driver-app-coordinator',daemon=True).start()
