"""Optional real-ORM checks. SQLite is NOT a PostgreSQL lock/concurrency test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt
import importlib.util
import re
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load, relative_modules

HAS_SQLALCHEMY=importlib.util.find_spec('sqlalchemy') is not None

@unittest.skipUnless(HAS_SQLALCHEMY,'Install SQLAlchemy to run real-ORM tests')
class RealOrmTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from common.models import Base,OutboxMessage,Order,User
        self.OutboxMessage,self.Order,self.User=OutboxMessage,Order,User
        self.engine=create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.Session=sessionmaker(self.engine,expire_on_commit=False)
        self.depth=0
        with self.Session.begin() as s:
            u=User(vk_id=100);s.add(u);s.flush()
            row=OutboxMessage(peer_id=100,text='Оцените поездку',keyboard='{"buttons":[[]],"inline":true}',
                              status='sent',random_id=123,last_error='vk_message_id:5;vk_cmid:6')
            s.add(row);s.flush();self.row_id=row.id
            o=Order(passenger_id=u.id,address_from='A',address_to='B',status='completed',driver_rating_prompt_outbox_id=row.id)
            s.add(o);s.flush();self.order_id=o.id

    def tearDown(self):self.engine.dispose()

    @contextmanager
    def session_scope(self):
        self.depth+=1
        try:
            with self.Session.begin() as s:yield s
        finally:self.depth-=1

    def outbox_scope(self):
        env={'__package__':'v173_runtime','OutboxMessage':self.OutboxMessage,'session_scope':self.session_scope,
             'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)),'dt':dt,'re':re,'log':Mock()}
        for name in ('_stored_vk_marker_id','_stored_vk_message_id','_stored_vk_cmid','has_usable_vk_id',
                     'finalize_tracked_message','_finalize_one'):
            load('bot/outbox_service.py',name,env)
        return env

    def test_expiry_commits_server_decision_and_queues_ui_edit(self):
        outbox=self.outbox_scope();service=NS(finalize_tracked_message=outbox['finalize_tracked_message'])
        env={'session_scope':self.session_scope,'Order':self.Order,'OutboxMessage':self.OutboxMessage,
             'outbox_service':service,'EXPIRY_TEXT':'Время для оценки истекло.',
             '_field':lambda side:'driver_rating_prompt_outbox_id'}
        load('bot/rating_expiry_service.py','expire',env)(self.order_id,driver_side=True)
        with self.Session() as s:
            self.assertIsNone(s.get(self.Order,self.order_id).driver_rating_prompt_outbox_id)
            row=s.get(self.OutboxMessage,self.row_id)
            self.assertEqual(row.status,'finalize_requested')
            self.assertIn('rating_expiry_retry:0',row.last_error)
        # Replayed timer is idempotent.
        service.finalize_tracked_message=Mock(side_effect=AssertionError('must not repeat'))
        load('bot/rating_expiry_service.py','expire',env)(self.order_id,driver_side=True)

    def test_real_row_flood_backoff_and_terminal_permission(self):
        outbox=self.outbox_scope()
        with self.session_scope() as s:
            outbox['finalize_tracked_message'](s,self.row_id,'Время для оценки истекло.',defer_edit=True)
            s.get(self.OutboxMessage,self.row_id).status='finalizing'
        vk=Mock();vk.last_edit_error_code.return_value=9
        def edit(*args):
            self.assertEqual(self.depth,0)
            return False
        vk.edit_message.side_effect=edit
        mod=types.ModuleType('v173_runtime.vk_client');mod.vk=vk
        with relative_modules(vk_client=mod):outbox['_finalize_one'](self.row_id)
        with self.session_scope() as s:
            row=s.get(self.OutboxMessage,self.row_id)
            self.assertEqual(row.status,'finalize_requested');self.assertIn('rating_expiry_retry:1',row.last_error)
            self.assertIsNone(row.claimed_at);row.status='finalizing'
        vk.last_edit_error_code.return_value=901
        with relative_modules(vk_client=mod):outbox['_finalize_one'](self.row_id)
        with self.Session() as s:
            row=s.get(self.OutboxMessage,self.row_id)
            self.assertEqual(row.status,'sent');self.assertIn('edit_abandoned_code:901',row.last_error)

    def test_active_send_is_excluded_from_cleanup_claims(self):
        with self.session_scope() as s:
            row=s.get(self.OutboxMessage,self.row_id)
            row.status='finalize_requested'
            row.next_attempt_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)
        env=self.outbox_scope()
        claim=load('bot/outbox_service.py','_claim_finalize_batch',env)
        self.assertEqual(claim(2,exclude_ids=(self.row_id,)),[])
        self.assertEqual(claim(2),[self.row_id])

if __name__=='__main__':unittest.main()
