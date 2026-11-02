"""V175: one queue snapshot, live front notices, bounded admin recovery."""
from __future__ import annotations
import ast
import datetime as dt
import http.client
import os
import signal
import subprocess
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from test_requirements_v173_requested_changes import load, node, relative_modules, source

class Field:
    def __init__(self,name='field'):self.name=name
    def in_(self,*args):return self
    def isnot(self,*args):return self
    def __eq__(self,value):return self
    def __gt__(self,value):return self

class QueueSnapshotV175(unittest.TestCase):
    def fixture(self, offered=(), held=()):
        users={id:NS(id=id,vk_id=100+id,is_on_line=True,driver_status='online',is_blocked=False,
                     has_role=lambda role:True) for id in (1,2,3)}
        # Physical queue position is not free rank: driver 3 has an active ride.
        rows=[NS(id=13,driver_id=3,position=1,city_id=10,status='waiting'),
              NS(id=11,driver_id=1,position=2,city_id=10,status='waiting'),
              NS(id=12,driver_id=2,position=3,city_id=10,status='waiting')]
        Order=NS(driver_id=Field('busy'),parallel_driver_id=Field('parallel'),
                 offered_driver_id=Field('offered'),status=Field())
        DriverQueue=type('DriverQueue',(),{'driver_id':Field()})
        DriverSelection=NS(driver_id=Field('held'),expires_at=Field())
        User=type('User',(),{})
        session=Mock()
        def query(model):
            q=Mock()
            if model is DriverQueue:
                q.all.return_value=rows;q.filter.return_value.all.return_value=rows
            else:
                mapping={'busy':[(3,)],'parallel':[], 'offered':[(id,) for id in offered], 'held':[(id,) for id in held]}
                q.filter.return_value.all.return_value=mapping.get(model.name,[])
            return q
        session.query.side_effect=query;session.get.side_effect=lambda model,id:users[id]
        scope={'Order':Order,'DriverQueue':DriverQueue,'DriverSelection':DriverSelection,'User':User,
               'ROLE_DRIVER':'driver','_QUEUE_STATUS_ORDER':{'waiting':0,'offered':1,'away':2,'assigned':3},
               '_driver_has_active_work':lambda s,id:id==3}
        for name in ('actual_driver_statuses','queue_entries','free_queue_snapshot','driver_queue_rank','driver_line_rank'):
            load('bot/queue_service.py',name,scope)
        return users,rows,session,scope

    def test_screenshot_second_free_driver_is_second_not_third(self):
        users,rows,session,scope=self.fixture()
        entries=scope['queue_entries'](session)
        self.assertEqual([(e['driver'].id,e['status']) for e in entries],[(1,'waiting'),(2,'waiting'),(3,'assigned')])
        self.assertEqual(scope['driver_queue_rank'](session,users[2],entries),2)
        self.assertEqual(scope['driver_line_rank'](session,users[2],entries),2)
        self.assertIsNone(scope['driver_queue_rank'](session,users[3],entries))
        self.assertEqual([r.driver_id for r in rows],[3,1,2]) # No queue relocation.

    def test_real_offer_is_not_free_even_when_raw_row_is_waiting(self):
        users,_,session,scope=self.fixture(offered=(1,))
        entries=scope['queue_entries'](session)
        self.assertEqual(next(e for e in entries if e['driver'].id==1)['status'],'offered')
        self.assertEqual(scope['driver_queue_rank'](session,users[2],entries),1)
        self.assertIsNone(scope['driver_queue_rank'](session,users[1],entries))

    def test_personal_selection_hold_is_not_free_or_first(self):
        users,_,session,scope=self.fixture(held=(1,))
        entries=scope['queue_entries'](session)
        self.assertIsNone(scope['driver_line_rank'](session,users[1],entries))
        self.assertEqual(scope['driver_line_rank'](session,users[2],entries),1)

    def test_offline_away_and_blocked_rows_do_not_inflate_free_rank(self):
        users,rows,session,scope=self.fixture()
        users[1].is_on_line=False
        entries=scope['queue_entries'](session)
        self.assertEqual(scope['driver_queue_rank'](session,users[2],entries),1)
        users[1].is_on_line=True;users[1].is_blocked=True
        self.assertEqual(scope['driver_queue_rank'](session,users[2],scope['queue_entries'](session)),1)

    def test_global_and_line_ranks_remain_distinct_but_screen_uses_line(self):
        users,rows,session,scope=self.fixture()
        rows[1].city_id=20
        entries=scope['queue_entries'](session)
        self.assertEqual(scope['driver_queue_rank'](session,users[2],entries),2)
        self.assertEqual(scope['driver_line_rank'](session,users[2],entries),1)
        screen=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','show_queue'))
        self.assertIn('driver_line_rank(session, user, entries=entries)',screen)
        self.assertNotIn('driver_queue_rank',screen)
        self.assertEqual(screen.count('queue_service.queue_entries(session)'),1)
        self.assertNotIn('queue_service.actual_driver_statuses(',screen)

    def test_snapshot_rank_does_not_query_a_second_raw_queue(self):
        users,_,session,scope=self.fixture()
        entries=scope['queue_entries'](session)
        before=session.query.call_count
        scope['driver_queue_rank'](session,users[2],entries)
        scope['driver_line_rank'](session,users[2],entries)
        self.assertEqual(session.query.call_count,before)

class FrontNoticeV175(unittest.TestCase):
    def guard(self, status='sending', valid=False, priority=901, text='Настроенное сообщение'):
        entry=NS(id=12,driver_id=2,front_notified=True,front_notice_outbox_id=99)
        row=NS(id=99,peer_id=102,priority=priority,text=text,status=status,claimed_at=1,last_error='vk_message_id:123;vk_cmid:321')
        session=Mock();session.query.return_value.filter.return_value.one_or_none.return_value=entry
        service=types.ModuleType('v173_runtime.queue_service');service.is_current_queue_front=Mock(return_value=valid)
        scope={'__package__':'v173_runtime','DriverQueue':NS(front_notice_outbox_id=Field()),
               'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)),'log':Mock()}
        with relative_modules(queue_service=service):
            result=load('bot/outbox_service.py','_cancel_if_queue_front_is_stale',scope)(session,row)
        return result,row,entry,session

    def test_second_driver_pending_first_notice_is_cancelled_before_send(self):
        result,row,entry,_=self.guard()
        self.assertTrue(result);self.assertEqual(row.status,'cancelled')
        self.assertFalse(entry.front_notified);self.assertIsNone(entry.front_notice_outbox_id)

    def test_valid_first_driver_notice_is_delivered(self):
        result,row,entry,_=self.guard(valid=True)
        self.assertFalse(result);self.assertEqual(row.status,'sending')
        self.assertEqual(entry.front_notice_outbox_id,99)

    def test_position_change_during_vk_send_requests_deletion(self):
        result,row,entry,_=self.guard(status='sent')
        self.assertTrue(result);self.assertEqual(row.status,'delete_requested')
        self.assertIn('vk_message_id:123',row.last_error)
        self.assertIsNotNone(row.next_attempt_at)

    def test_legacy_first_notice_is_checked(self):
        self.assertTrue(self.guard(priority=900,text='🥇 Вы первый в очереди! Следующая заявка ваша.')[0])

    def test_normal_payment_and_chat_messages_are_not_suppressed(self):
        for priority,text in ((5000,'Реквизиты для оплаты'),(900,'Сообщение клиента: Вы первый в очереди!'),(0,'Меню')):
            result,row,_,session=self.guard(priority=priority,text=text)
            self.assertFalse(result);session.query.assert_not_called()

    def test_guard_is_wired_before_and_after_actual_delivery(self):
        text=ast.get_source_segment(source('bot/outbox_service.py'),node('bot/outbox_service.py','_deliver_one'))
        self.assertEqual(text.count('_cancel_if_queue_front_is_stale(session, row)'),2)
        self.assertLess(text.index('_cancel_if_queue_front_is_stale'),text.index('vk._send_now_result'))
        self.assertGreater(text.rindex('_cancel_if_queue_front_is_stale'),text.index('row.status = "finalize_requested" if finalize_after_send else "sent"'))

    def test_live_front_check_rejects_second_and_waiting_assignment(self):
        city=NS(name='Горнозаводск');session=Mock();session.get.return_value=city
        entry=NS(id=12,driver_id=2,city_id=10)
        snapshot=[{'queue_id':11,'city_id':10},{'queue_id':12,'city_id':10}]
        scope={'City':object,'free_queue_snapshot':lambda s:snapshot,
               '_driver_has_active_work':lambda *a:False,'_line_has_waiting_assignment':lambda *a:False}
        check=load('bot/queue_service.py','is_current_queue_front',scope)
        self.assertFalse(check(session,entry))
        entry.id=11;self.assertTrue(check(session,entry))
        scope['_line_has_waiting_assignment']=lambda *a:True
        self.assertFalse(check(session,entry))

    def test_notifications_use_same_snapshot_and_flush_dirty_transitions(self):
        users={id:NS(id=id,vk_id=100+id,is_on_line=True,driver_status='online',is_blocked=False) for id in (1,2,3)}
        rows=[NS(id=13,driver_id=3,city_id=10,front_notified=True,front_notice_outbox_id=97),
              NS(id=11,driver_id=1,city_id=10,front_notified=False,front_notice_outbox_id=None),
              NS(id=12,driver_id=2,city_id=10,front_notified=True,front_notice_outbox_id=99)]
        city=NS(id=10)
        City=NS(name=Field());DriverQueue=type('DQ',(),{});User=object
        session=Mock()
        def query(model):
            q=Mock()
            if model is City:q.filter.return_value.one_or_none.return_value=city
            else:q.all.return_value=rows
            return q
        session.query.side_effect=query;session.get.side_effect=lambda model,id:users[id]
        cleared=[]
        def clear(s,e):
            cleared.append(e.driver_id);e.front_notified=False;e.front_notice_outbox_id=None
        scope={'City':City,'DriverQueue':DriverQueue,'User':User,'log':Mock(),
               'free_queue_snapshot':lambda s:[{'queue_id':11,'city_id':10},{'queue_id':12,'city_id':10}],
               '_driver_has_active_work':lambda s,id:id==3,'_line_has_waiting_assignment':lambda *a:False,
               '_clear_front_notice':clear,'__package__':'v173_runtime'}
        settings=types.ModuleType('common.settings_service');settings.msg=lambda *a:'🥇 Вы первый в очереди!'
        client=types.ModuleType('v173_runtime.vk_client');client.vk=Mock();client.vk.send_tracked_message.return_value=1000
        with patch.dict(sys.modules,{'common.settings_service':settings}),relative_modules(vk_client=client):
            load('bot/queue_service.py','_notify_fronts',scope)(session)
        self.assertEqual(cleared,[3,2]);session.flush.assert_called_once()
        client.vk.send_tracked_message.assert_called_once_with(101,'🥇 Вы первый в очереди!',priority=901)
        self.assertEqual(rows[1].front_notice_outbox_id,1000)

class AdminResilienceV175(unittest.TestCase):
    def supervisor(self):
        from common import admin_supervisor
        return admin_supervisor

    def test_liveness_is_separate_from_database_readiness(self):
        self.assertEqual(load('web/app.py','live')(),({'status':'alive'},200))
        live_source=ast.get_source_segment(source('web/app.py'),node('web/app.py','live'))
        self.assertNotIn('execute(',live_source)
        self.assertIn('SELECT 1',ast.get_source_segment(source('web/app.py'),node('web/app.py','health')))

    def test_launcher_does_not_import_flask_or_run_schema_before_bind(self):
        text=source('admin_main.py')
        self.assertNotIn('from web.app import app',text)
        self.assertNotIn('ensure_schema',text)
        self.assertIn('run_supervised',text)

    def test_command_has_bounded_worker_limits_and_valid_port(self):
        supervisor=self.supervisor()
        scope={'os':os,'sys':sys,'positive_env':supervisor.positive_env}
        with patch.dict(os.environ,{'PORT':'3000','WEB_WORKERS':'1','WEB_THREADS':'4'},clear=True):
            args=load('admin_main.py','gunicorn_command',scope)()
        self.assertEqual(args[args.index('--bind')+1],'0.0.0.0:3000')
        self.assertEqual(args[args.index('--worker-class')+1],'gthread')
        self.assertEqual(args[args.index('--max-requests')+1],'2000')
        self.assertIn('--graceful-timeout',args)

    def test_invalid_numeric_env_falls_back_or_clamps(self):
        sup=self.supervisor()
        with patch.dict(os.environ,{'TEST_VALUE':'bad'}):self.assertEqual(sup.positive_env('TEST_VALUE',15),15)
        with patch.dict(os.environ,{'TEST_VALUE':'0'}):self.assertEqual(sup.positive_env('TEST_VALUE',15),1)

    def test_probe_is_bounded_direct_local_http_without_proxy_env(self):
        sup=self.supervisor();connection=Mock();response=Mock(status=200);connection.getresponse.return_value=response
        with patch.object(sup.http.client,'HTTPConnection',return_value=connection) as factory:
            self.assertEqual(sup.probe_liveness(3000,3),(True,'HTTP 200'))
        factory.assert_called_once_with('127.0.0.1',3000,timeout=3)
        connection.request.assert_called_once_with('GET','/live');connection.close.assert_called_once()

    def test_failed_probe_closes_socket_and_reports_reason(self):
        sup=self.supervisor();connection=Mock();connection.request.side_effect=TimeoutError('timed out')
        with patch.object(sup.http.client,'HTTPConnection',return_value=connection):
            healthy,detail=sup.probe_liveness(3000,3)
        self.assertFalse(healthy);self.assertIn('TimeoutError',detail);connection.close.assert_called_once()

    def test_hung_server_restarts_after_threshold_not_single_failure(self):
        sup=self.supervisor();first=Mock(pid=100);second=Mock(pid=101)
        first.poll.return_value=None;second.poll.return_value=None
        stop=Mock();stop.wait.side_effect=[False,False,False,False,True]
        with patch.dict(os.environ,{'ADMIN_WATCHDOG_INTERVAL':'1','ADMIN_WATCHDOG_FAILURES':'2','ADMIN_WATCHDOG_STARTUP_GRACE':'1'},clear=True),\
             patch.object(sup,'supervisor_logger',return_value=Mock()),patch.object(sup.threading,'Event',return_value=stop),\
             patch.object(sup.signal,'signal'),patch.object(sup.time,'monotonic',side_effect=[0,100,102,103,200]),\
             patch.object(sup.subprocess,'Popen',side_effect=[first,second]) as launch,\
             patch.object(sup,'probe_liveness',side_effect=[(False,'timeout'),(False,'timeout'),(True,'HTTP 200')]) as probe,\
             patch.object(sup,'stop_server') as terminate:
            self.assertEqual(sup.run_supervised(['gunicorn'],3000),0)
        self.assertEqual(launch.call_count,2);self.assertEqual(probe.call_count,3)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual([call.args[0] for call in terminate.call_args_list],[first,second])

    def test_one_transient_failure_does_not_restart(self):
        sup=self.supervisor();process=Mock(pid=100);process.poll.return_value=None
        stop=Mock();stop.wait.side_effect=[False,False,True]
        with patch.dict(os.environ,{'ADMIN_WATCHDOG_INTERVAL':'1','ADMIN_WATCHDOG_FAILURES':'2','ADMIN_WATCHDOG_STARTUP_GRACE':'1'},clear=True),\
             patch.object(sup,'supervisor_logger',return_value=Mock()),patch.object(sup.threading,'Event',return_value=stop),\
             patch.object(sup.signal,'signal'),patch.object(sup.time,'monotonic',side_effect=[0,100,102]),\
             patch.object(sup.subprocess,'Popen',return_value=process) as launch,\
             patch.object(sup,'probe_liveness',side_effect=[(False,'timeout'),(True,'HTTP 200')]),patch.object(sup,'stop_server'):
            self.assertEqual(sup.run_supervised(['gunicorn'],3000),0)
        launch.assert_called_once()

    def test_graceful_stop_signals_only_gunicorn_group(self):
        sup=self.supervisor();process=Mock(pid=100);process.poll.return_value=None
        with patch.object(sup.os,'killpg') as kill:
            sup.stop_server(process,Mock())
        kill.assert_called_once_with(100,signal.SIGTERM);process.wait.assert_called_once_with(timeout=30)

    def test_unresponsive_shutdown_escalates_only_own_group(self):
        sup=self.supervisor();process=Mock(pid=100);process.poll.return_value=None
        process.wait.side_effect=[subprocess.TimeoutExpired('gunicorn',30),0]
        with patch.object(sup.os,'killpg') as kill:
            sup.stop_server(process,Mock())
        self.assertEqual([c.args for c in kill.call_args_list],[(100,signal.SIGTERM),(100,signal.SIGKILL)])

    def test_admin_sql_limits_are_bounded_and_bot_defaults_are_not_changed(self):
        config=NS(sqlalchemy_url=lambda:'postgresql+psycopg2://db',DB_CONNECT_TIMEOUT=5,
                  DB_STATEMENT_TIMEOUT_MS=30000,DB_LOCK_TIMEOUT_MS=5000,DB_IDLE_TRANSACTION_TIMEOUT_MS=60000)
        args=load('common/database.py','_connection_args',{'config':config})()
        self.assertEqual(args['connect_timeout'],5)
        self.assertIn('statement_timeout=30000',args['options'])
        self.assertIn('lock_timeout=5000',args['options'])
        config.DB_STATEMENT_TIMEOUT_MS=0;config.DB_LOCK_TIMEOUT_MS=0;config.DB_IDLE_TRANSACTION_TIMEOUT_MS=0
        self.assertEqual(load('common/database.py','_connection_args',{'config':config})(),{'connect_timeout':5})
        config.sqlalchemy_url=lambda:'sqlite://'
        self.assertEqual(load('common/database.py','_connection_args',{'config':config})(),{})

    def test_docker_skips_repeat_worker_migrations_but_keeps_migrate_dependency(self):
        text=source('docker-compose.yml').split('  admin:',1)[1]
        self.assertIn('AUTO_MIGRATE_ON_START: "0"',text)
        self.assertIn('condition: service_completed_successfully',text)
        self.assertIn('ADMIN_WATCHDOG_ENABLED',text)
        self.assertIn('stop_grace_period: 60s',text)
        self.assertIn('os.getenv("AUTO_MIGRATE_ON_START", "1")',source('web/app.py'))

class AdditionalRegressionV175(unittest.TestCase):
    def test_duplicate_positions_have_same_stable_visible_and_dispatch_order(self):
        fixture=QueueSnapshotV175()
        users,rows,session,scope=fixture.fixture()
        rows[1].position=2;rows[2].position=2
        rows[1:]=list(reversed(rows[1:]))
        entries=scope['queue_entries'](session)
        self.assertEqual([e['driver'].id for e in entries[:2]],[1,2])
        self.assertEqual(scope['driver_queue_rank'](session,users[2],entries),2)
        text=ast.get_source_segment(source('bot/queue_service.py'),node('bot/queue_service.py','next_waiting_driver'))
        self.assertIn('DriverQueue.position.asc(), DriverQueue.id.asc()',text)

    def test_startup_grace_does_not_restart_worker_still_initializing(self):
        from common import admin_supervisor as sup
        process=Mock(pid=100);process.poll.return_value=None
        stop=Mock();stop.wait.side_effect=[False,True]
        with patch.dict(os.environ,{'ADMIN_WATCHDOG_STARTUP_GRACE':'120'},clear=True),\
             patch.object(sup,'supervisor_logger',return_value=Mock()),patch.object(sup.threading,'Event',return_value=stop),\
             patch.object(sup.signal,'signal'),patch.object(sup.time,'monotonic',side_effect=[0,30]),\
             patch.object(sup.subprocess,'Popen',return_value=process) as launch,\
             patch.object(sup,'probe_liveness') as probe,patch.object(sup,'stop_server'):
            self.assertEqual(sup.run_supervised(['gunicorn'],3000),0)
        launch.assert_called_once();probe.assert_not_called()

    def test_unexpected_exit_is_returned_for_docker_restart_policy(self):
        from common import admin_supervisor as sup
        process=Mock(pid=100);process.poll.return_value=3
        stop=Mock();stop.wait.return_value=False
        with patch.object(sup,'supervisor_logger',return_value=Mock()),patch.object(sup.threading,'Event',return_value=stop),\
             patch.object(sup.signal,'signal'),patch.object(sup.time,'monotonic',return_value=0),\
             patch.object(sup.subprocess,'Popen',return_value=process),patch.object(sup,'stop_server'),\
             patch.object(sup,'probe_liveness') as probe:
            self.assertEqual(sup.run_supervised(['gunicorn'],3000),3)
        probe.assert_not_called()


class QueueFirstWordingV175(unittest.TestCase):
    def test_old_factory_template_clarifies_line(self):
        old = '🥇 Вы первый в очереди! Следующая заявка ваша — ожидайте заявку.'
        new = '🥇 Вы первый в очереди на линии «Горнозаводск». Ожидайте подходящую заявку.'
        scope = {'get_cached': lambda *a: old, 'DEFAULTS': {'msg_queue_first': new}}
        message = load('common/settings_service.py', 'msg', scope)
        self.assertEqual(message(None, 'msg_queue_first'), new)
        self.assertIn(new, source('common/settings_service.py'))

    def test_custom_template_is_preserved(self):
        scope = {'get_cached': lambda *a: 'Мой текст', 'DEFAULTS': {'msg_queue_first': 'Новый текст'}}
        self.assertEqual(load('common/settings_service.py', 'msg', scope)(None, 'msg_queue_first'), 'Мой текст')
