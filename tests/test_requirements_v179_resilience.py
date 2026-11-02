"""V179: executable failure-path tests; no production credentials or services."""
from __future__ import annotations
import datetime as dt
import importlib.util
import os
import re
import sys
import threading
import time
import types
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from test_requirements_v173_requested_changes import load, node, relative_modules, source

ROOT = Path(__file__).resolve().parents[1]

class Field:
    def __eq__(self, other): return self

class FakeError(Exception):
    def __init__(self, code): self.code=code; super().__init__(f'[{code}] test')

class VkEditTests(unittest.TestCase):
    def client(self, error=None):
        scope={'threading':threading,'config':NS(VK_GROUP_ID=1),'log':Mock(),'re':re}
        load('bot/vk_client.py','_vk_error_code',scope)
        cls=load('bot/vk_client.py','VKClient',scope)
        client=object.__new__(cls);client._message_local=threading.local()
        client._uses_chat_user_token=lambda peer:False
        api=NS(messages=NS(edit=Mock(return_value=1,side_effect=error)))
        client._message_api_for_peer=lambda peer:api
        return client,api.messages.edit

    def test_flood_does_not_try_four_id_variants(self):
        client,edit=self.client(FakeError(9))
        self.assertFalse(client.edit_message(1,5,'x',conversation_message_id=6))
        self.assertEqual(edit.call_count,1);self.assertEqual(client.last_edit_error_code(),9)

    def test_permissions_do_not_try_four_id_variants(self):
        for code in (901,911,29):
            client,edit=self.client(FakeError(code))
            self.assertFalse(client.edit_message(1,5,'x',conversation_message_id=6))
            self.assertEqual(edit.call_count,1);self.assertEqual(client.last_edit_error_code(),code)

    def test_success_resets_previous_thread_error(self):
        client,edit=self.client();client._message_local.last_edit_error_code=9
        self.assertTrue(client.edit_message(1,5,'x',attachment='photo1_2'))
        self.assertIsNone(client.last_edit_error_code())
        self.assertEqual(edit.call_args.kwargs['attachment'],'photo1_2')

    def test_vk_sessions_have_finite_timeouts(self):
        import ast
        calls=[n for n in ast.walk(ast.parse(source('bot/vk_client.py')))
               if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='VkApi']
        self.assertEqual(len(calls),4)
        for call in calls:self.assertTrue(any(k.arg=='session' and isinstance(k.value, ast.Call) and k.value.func.id=='BoundedSession' for k in call.keywords))

class NetworkTimeoutTests(unittest.TestCase):
    def session(self):
        class Base:
            def request(self, method, url, **kwargs): return kwargs
        cls=load('bot/vk_client.py','BoundedSession',{'requests':NS(Session=Base)})
        return cls()

    def test_api_without_timeout_gets_finite_connect_and_read(self):
        self.assertEqual(self.session().request('POST','https://example.test')['timeout'],(5,15))

    def test_explicit_timeout_is_not_shortened(self):
        self.assertEqual(self.session().request('GET','https://example.test',timeout=35)['timeout'],35)


class RatingFinalizeTests(unittest.TestCase):
    def fixture(self,code=9,attempts=0,success=False):
        row=NS(id=12,status='finalizing',peer_id=101,text='Время для оценки истекло.',keyboard='{"buttons":[],"inline":true}',
               attachment='photo1_2',last_error=f'vk_message_id:5;vk_cmid:6;rating_expiry_retry:{attempts}',
               attempts=0,claimed_at=1)
        session=Mock();session.get.return_value=row
        session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=row
        depth=[0];network_checks=[]
        @contextmanager
        def scope():
            depth[0]+=1
            try:yield session
            finally:depth[0]-=1
        vk=Mock();vk.last_edit_error_code.return_value=code
        def perform(*args):network_checks.append(depth[0]);return success
        vk.edit_message.side_effect=perform
        module=types.ModuleType('v173_runtime.vk_client');module.vk=vk
        scope_dict={'__package__':'v173_runtime','session_scope':scope,'OutboxMessage':NS(id=Field()),
                    '_stored_vk_message_id':lambda r:5,'_stored_vk_cmid':lambda r:6,
                    'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)),'dt':dt,'re':re,'log':Mock()}
        with relative_modules(vk_client=module):load('bot/outbox_service.py','_finalize_one',scope_dict)(12)
        return row,vk,network_checks

    def test_network_edit_runs_after_transaction_closed(self):
        row,vk,checks=self.fixture();self.assertEqual(checks,[0])
        self.assertEqual(vk.edit_message.call_args.args[-1],'photo1_2')

    def test_flood_retries_later_not_every_15_seconds(self):
        before=dt.datetime.now(dt.timezone.utc);row,_,_=self.fixture()
        self.assertEqual(row.status,'finalize_requested')
        self.assertGreaterEqual((row.next_attempt_at-before).total_seconds(),60)
        self.assertIn('rating_expiry_retry:1',row.last_error)

    def test_permission_failure_stops_cosmetic_retry(self):
        for code in (901,911):
            row,_,_=self.fixture(code=code)
            self.assertEqual(row.status,'sent');self.assertIn('edit_abandoned_code',row.last_error)

    def test_eighth_rating_edit_failure_stops_retry(self):
        row,_,_=self.fixture(attempts=7)
        self.assertEqual(row.status,'sent');self.assertIn('rating_expiry_retry:8',row.last_error)

    def test_success_preserves_vk_ids_and_finishes(self):
        row,_,_=self.fixture(success=True)
        self.assertEqual(row.status,'sent');self.assertIn('vk_cmid:6',row.last_error)
        self.assertIn('finalized_without_keyboard',row.last_error)

    def test_deferred_finalization_never_calls_vk_in_rating_transaction(self):
        row=NS(status='sent',text='Оцените поездку',keyboard='{"buttons":[[]],"inline":true}',attachment=None,
               last_error='vk_message_id:5;vk_cmid:6')
        session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=row
        scope={'OutboxMessage':NS(id=Field()),'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)), 'has_usable_vk_id':lambda row:True}
        result=load('bot/outbox_service.py','finalize_tracked_message',scope)(session,12,'Время для оценки истекло.',defer_edit=True)
        self.assertTrue(result);self.assertEqual(row.status,'finalize_requested')
        self.assertIn('rating_expiry_retry:0',row.last_error)
        self.assertEqual(row.keyboard,'{"buttons":[],"inline":true}')

    def test_expiry_clears_server_prompt_without_waiting_for_vk(self):
        order=NS(id=10335,passenger_rating_prompt_outbox_id=12,passenger_id=None)
        row=NS(text='Оцените',keyboard='{"buttons":[[]],"inline":true}',status='sent')
        session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
        session.get.return_value=row
        @contextmanager
        def scope():yield session
        outbox=NS(finalize_tracked_message=Mock(return_value=True))
        env={'_field':lambda side:'passenger_rating_prompt_outbox_id','session_scope':scope,'Order':NS(id=Field()),
             'OutboxMessage':object,'outbox_service':outbox,'EXPIRY_TEXT':'Время для оценки истекло.'}
        load('bot/rating_expiry_service.py','expire',env)(10335,driver_side=False)
        self.assertIsNone(order.passenger_rating_prompt_outbox_id)
        self.assertTrue(outbox.finalize_tracked_message.call_args.kwargs['defer_edit'])

class PushTests(unittest.TestCase):
    def send(self,error):
        class UnregisteredError(Exception):pass
        device=NS(id=17,is_active=True,token='test')
        scenario=NS(enabled=True,title='Новая заявка',duration_seconds=60,repeat_seconds=5,vibrate=True,sound_file=None)
        session=Mock();session.query.return_value.filter_by.return_value.first.return_value=scenario
        session.query.return_value.filter_by.return_value.all.return_value=[device]
        msg=NS(UnregisteredError=UnregisteredError,send=Mock(side_effect=UnregisteredError() if error=='unregistered' else error),
               Message=lambda **kw:kw,AndroidConfig=lambda **kw:kw)
        firebase=types.ModuleType('firebase_admin');firebase.messaging=msg
        scope={'ensure_scenarios':lambda s:None,'DriverPushDevice':object,'PushScenario':object,
               '_firebase':lambda:True,'os':os,'log':Mock()}
        with patch.dict(sys.modules,{'firebase_admin':firebase}):result=load('bot/push_service.py','send',scope)(session,7,'new_order')
        return device,result

    def test_unregistered_device_is_disabled(self):
        device,result=self.send('unregistered');self.assertFalse(device.is_active);self.assertEqual(result,0)

    def test_transient_failure_does_not_disable_device(self):
        device,result=self.send(OSError('network'));self.assertTrue(device.is_active);self.assertEqual(result,0)

class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('isolated_guard',ROOT/'bot/runtime_guard.py')
        self.guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.guard)

    def test_quiet_bot_healthy_when_poll_requests_complete(self):
        g=self.guard;g._ready=True;g._pulses={'longpoll':100,'outbox-loop':100}
        self.assertIsNone(g.stalled_reason(120))

    def test_stalled_longpoll_detected_without_user_messages(self):
        g=self.guard;g._ready=True;g._pulses={'longpoll':0,'outbox-loop':100}
        self.assertIn('longpoll',g.stalled_reason(100))

    def test_stalled_outbox_coordinator_detected(self):
        g=self.guard;g._ready=True;g._pulses={'longpoll':100,'outbox-loop':0}
        self.assertIn('outbox-loop',g.stalled_reason(100))

    def test_running_task_deadline(self):
        g=self.guard;g._ready=True;g._active={1:('event','test',0)}
        self.assertIn('event',g.stalled_reason(200))

    def test_startup_has_separate_grace(self):
        g=self.guard;g._startup_at=0
        self.assertIsNone(g.stalled_reason(100));self.assertIn('startup',g.stalled_reason(200))

    def test_completed_or_failed_call_is_removed(self):
        g=self.guard;self.assertEqual(g.call('event','x',lambda:42),42)
        self.assertEqual(g._active,{})
        with self.assertRaises(ValueError):g.call('event','x',lambda:(_ for _ in ()).throw(ValueError()))
        self.assertEqual(g._active,{})

    def test_disabled_guard_never_starts_thread(self):
        with patch.dict(os.environ,{'BOT_WATCHDOG_ENABLED':'0'}):self.guard.start()
        self.assertFalse(self.guard._started)

    def test_watchdog_dump_then_exit_and_no_periodic_reboot(self):
        g=self.guard
        with patch.object(g,'stalled_reason',side_effect=['stuck','stuck']),patch.object(g.time,'sleep'), \
             patch.object(g.faulthandler,'dump_traceback') as dump,patch.object(g.os,'_exit',side_effect=SystemExit(70)) as ex:
            with self.assertRaises(SystemExit):g._monitor()
            dump.assert_called_once();ex.assert_called_once_with(70)

    def test_recovery_cancelled_if_progress_returns(self):
        g=self.guard
        class Stop(BaseException):pass
        with patch.object(g,'stalled_reason',side_effect=['stuck',None]),patch.object(g.time,'sleep',side_effect=[None,None,Stop()]), \
             patch.object(g.faulthandler,'dump_traceback'),patch.object(g.os,'_exit') as ex:
            with self.assertRaises(Stop):g._monitor()
            ex.assert_not_called()

class OutboxConcurrencyTests(unittest.TestCase):
    def test_completed_slot_reused_while_another_send_is_stuck(self):
        class Stop(BaseException):pass
        blocked=threading.Event();third=threading.Event();sent=[];batches=[]
        def deliver(id):
            if id==1:blocked.wait(3)
            sent.append(id)
            if id==3:third.set()
        remaining=[1,2,3]
        def claim(limit):
            ids=remaining[:limit];del remaining[:limit];batches.append((limit,ids));return ids
        def sleep(delay):
            if third.is_set():raise Stop()
            time.sleep(.01)
        config=types.ModuleType('common.config');config.config=NS(OUTBOX_WORKERS=2)
        guard=types.ModuleType('v173_runtime.runtime_guard');guard.pulse=lambda name:None
        pool=ThreadPoolExecutor(max_workers=2)
        scope={'__package__':'v173_runtime','ThreadPoolExecutor':ThreadPoolExecutor,'_executor':pool,
               '_claim_batch':claim,'_claim_finalize_batch':lambda n,**kw:[],'_claim_delete_batch':lambda n,**kw:[],
               '_finalize_one':Mock(),'_delete_one':Mock(),'_deliver_one':deliver,
               '_run_outbox_task':lambda cb,id:cb(id),'time':NS(sleep=sleep),'log':Mock()}
        load('bot/outbox_service.py','_collect_done',scope)
        try:
            with patch.dict(sys.modules,{'common.config':config}),relative_modules(runtime_guard=guard):
                with self.assertRaises(Stop):load('bot/outbox_service.py','_worker',scope)()
            self.assertIn(3,sent);self.assertNotIn(1,sent)
            self.assertTrue(all(len(ids)<=limit for limit,ids in batches))
        finally:blocked.set();pool.shutdown(wait=True)

    def test_done_future_exception_does_not_block_other_slots(self):
        bad=Future();bad.set_exception(OSError('test'));good=Future();good.set_result(None);pending=Future()
        active={bad:1,good:2,pending:3}
        load('bot/outbox_service.py','_collect_done',{'log':Mock()})(active)
        self.assertEqual(active,{pending:3})

    def test_no_live_sending_reclaim_by_age(self):
        import ast
        part=ast.get_source_segment(source('bot/outbox_service.py'),node('bot/outbox_service.py','_claim_batch'))
        self.assertNotIn('claimed_at <',part)
        self.assertIn('.priority.desc()',part);self.assertIn('skip_locked=True',part)

class ScopeTests(unittest.TestCase):
    def test_compose_bot_only_timeouts_and_restart(self):
        text=source('docker-compose.yml');bot=text.split('  bot:\n',1)[1].split('  push-sounds-init:',1)[0]
        self.assertIn('restart: unless-stopped',bot);self.assertIn('BOT_DB_LOCK_TIMEOUT_MS',bot)
        self.assertIn('BOT_WATCHDOG_ENABLED',bot)
        admin=text.split('  admin:\n',1)[1];self.assertNotIn('BOT_TASK_TIMEOUT',admin)

if __name__=='__main__':unittest.main()
