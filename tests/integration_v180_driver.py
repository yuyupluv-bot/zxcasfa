"""Actual ORM + Flask tests, no VK/FCM calls. SQLite does NOT validate PG locks."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import datetime as dt
import hashlib
import importlib
import json
import tempfile
import types
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch,Mock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from flask import Flask,request
from common.models import Base,User,DriverPushDevice,DriverAppCommand,Setting,State,Order,OutboxMessage,ScheduledJob

UTC=dt.timezone.utc
class DriverTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.Session=sessionmaker(bind=self.engine,expire_on_commit=False)
        self.now=dt.datetime.now(UTC)
        self.secret='test-secret-long-enough-1234567890'
        with self.Session.begin() as s:
            u=User(vk_id=101,role='driver',granted_roles='passenger,driver');s.add(u);s.flush();self.driver_id=u.id
            d=DriverPushDevice(driver_id=u.id,token='test-fcm-token',device_secret_hash=hashlib.sha256(self.secret.encode()).hexdigest(),is_active=False)
            s.add(d);s.flush();self.device_id=d.id
            s.add(Setting(key='driver_app_worker_heartbeat',value=self.now.isoformat()))
            s.add(State(vk_id=101,state='driver_menu',data='{}'))
        self.auth={'device_id':self.device_id,'device_secret':self.secret}
        self.work=importlib.import_module('web.driver_work')
        self.app=Flask(__name__);self.app.register_blueprint(self.work.bp)
        @self.app.before_request
        def db():request.db=self.Session()
        @self.app.teardown_request
        def close(error):request.db.close()
        self.client=self.app.test_client()
    def tearDown(self):self.engine.dispose()
    def get_state(self):
        r=self.client.post('/api/driver-app/work/state',json=self.auth);self.assertEqual(r.status_code,200,r.data)
        return r.json
    def command(self,snapshot=None,**changes):
        snapshot=snapshot or self.get_state()
        body={**self.auth,'request_id':str(uuid.uuid4()),'revision':snapshot['revision'],'payload':{'cmd':'choose_line'}}
        body.update(changes);return body
    def post(self,body):return self.client.post('/api/driver-app/work/action',json=body)
    def test_push_inactive_is_not_login_revocation(self):
        self.assertTrue(self.get_state()['can_act'])
    def test_secret_and_role_are_required(self):
        self.assertEqual(self.client.post('/api/driver-app/work/state',json={**self.auth,'device_secret':'wrong-secret-but-long-enough'}).status_code,401)
        with self.Session.begin() as s:s.get(User,self.driver_id).is_blocked=True
        self.assertEqual(self.client.post('/api/driver-app/work/state',json=self.auth).status_code,403)
    def test_receipt_idempotency_and_body_conflict(self):
        body=self.command();self.assertEqual(self.post(body).status_code,202)
        self.assertEqual(self.post(body).status_code,200)
        with self.Session() as s:self.assertEqual(s.query(DriverAppCommand).count(),1)
        body['payload']={'cmd':'driver_offline'};self.assertEqual(self.post(body).status_code,409)
    def test_stale_and_arbitrary_commands_rejected(self):
        self.assertEqual(self.post(self.command(revision='0'*64)).status_code,409)
        self.assertEqual(self.post(self.command(payload={'cmd':'admin_delete_everything'})).status_code,403)
        self.assertEqual(self.post(self.command(text='123',payload={})).status_code,403)
    def test_down_worker_or_wrong_role_readonly(self):
        with self.Session.begin() as s:
            s.query(Setting).one().value=(self.now-dt.timedelta(minutes=1)).isoformat()
        self.assertFalse(self.get_state()['can_act']);self.assertEqual(self.post(self.command()).status_code,503)
        with self.Session.begin() as s:s.get(User,self.driver_id).role='passenger'
        state=self.get_state();self.assertFalse(state['actions']);self.assertIsNone(state['input_hint'])
    def test_bound_inflight_queue(self):
        for i in range(3):self.assertEqual(self.post(self.command()).status_code,202)
        self.assertEqual(self.post(self.command()).status_code,429)
    def test_invalid_payloads(self):
        for changes in ({'request_id':'invalid'},{'revision':'invalid'},{'text':'x','payload':{'cmd':'choose_line'}},{'payload':[],'text':'x'}):
            self.assertEqual(self.post(self.command(**changes)).status_code,400)
    def test_only_current_driver_orders(self):
        with self.Session.begin() as s:
            stranger=User(vk_id=202);s.add(stranger);s.flush()
            s.add(Order(passenger_id=stranger.id,driver_id=stranger.id,status='assigned',address_from='private address',address_to='private to'))
            s.add(Order(passenger_id=stranger.id,driver_id=self.driver_id,status='assigned',address_from='my pickup',address_to='to'))
        rows=self.get_state()['orders'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['from'],'my pickup')
    def test_expired_accept_hidden_and_malformed_old_keyboard_ignored(self):
        with self.Session.begin() as s:
            o=Order(passenger_id=self.driver_id,offered_driver_id=self.driver_id,status='searching',address_from='pickup',address_to='dropoff');s.add(o);s.flush()
            s.add(ScheduledJob(job_key=f'accept:{o.id}',kind='accept',object_id=o.id,run_at=self.now-dt.timedelta(seconds=10)))
            s.add(OutboxMessage(peer_id=101,text='offer',keyboard=json.dumps({'inline':True,'buttons':[[{'action':{'payload':json.dumps({'cmd':'accept','order_id':'not-a-number'})}}]]}),random_id=1))
        self.assertNotIn('accept',[a['payload']['cmd'] for a in self.get_state()['actions']])
    @contextmanager
    def session_scope(self):
        with self.Session.begin() as s:yield s
    def load_commands(self):
        fake=types.ModuleType('common.database');fake.session_scope=self.session_scope
        handler=types.ModuleType('bot.handlers');handler.handle_driver=Mock()
        states=types.ModuleType('bot.states_service');states.get_state=lambda s,vk:s.query(State).filter_by(vk_id=vk).one()
        with patch.dict(sys.modules,{'common.database':fake,'bot.handlers':handler,'bot.states_service':states}):
            sys.modules.pop('bot.driver_app_commands',None)
            m=importlib.import_module('bot.driver_app_commands')
        return m,handler,states
    def test_execute_delegates_to_bot_handler_once(self):
        body=self.command();self.post(body)
        with self.Session.begin() as s:c=s.query(DriverAppCommand).one();c.status='running';id=c.id
        module,handler,states=self.load_commands()
        # Lazy handler imports happen when executing, keeping the web process clean.
        with patch.dict(sys.modules,{'bot.handlers':handler,'bot.states_service':states}):module.execute(id);module.execute(id)
        handler.handle_driver.assert_called_once()
        args=handler.handle_driver.call_args.args
        self.assertEqual(args[2],'driver_menu');self.assertEqual(args[4],{'cmd':'choose_line'})
        with self.Session() as s:self.assertEqual(s.get(DriverAppCommand,id).status,'done')
    def test_expired_command_never_replayed(self):
        self.post(self.command())
        with self.Session.begin() as s:c=s.query(DriverAppCommand).one();c.status='running';c.expires_at=self.now-dt.timedelta(seconds=5);id=c.id
        module,handler,states=self.load_commands()
        with patch.dict(sys.modules,{'bot.handlers':handler,'bot.states_service':states}):module.execute(id)
        handler.handle_driver.assert_not_called()
        with self.Session() as s:self.assertEqual(s.get(DriverAppCommand,id).status,'rejected')
    def test_geo_scope_and_missing_data_fail_closed(self):
        fake=types.ModuleType('common.database');fake.get_session=self.Session
        with patch.dict(sys.modules,{'common.database':fake}):
            sys.modules.pop('web.driver_geo',None);geo=importlib.import_module('web.driver_geo')
        self.assertLess(geo.distance(geo.CENTER,(58.43,58.24)),50000)
        with tempfile.TemporaryDirectory() as d,patch.object(geo,'DATA',Path(d)):
            client=geo.app.test_client()
            self.assertEqual(client.get('/driver-map/style.json').status_code,503)
            self.assertEqual(client.post('/api/driver-app/geo/search',json={**self.auth,'query':'...'}).status_code,400)
            self.assertEqual(client.post('/api/driver-app/geo/route',json={**self.auth,'from':list(geo.CENTER),'to':[0,0]}).status_code,422)
            self.assertEqual(client.post('/api/driver-app/geo/route',json={**self.auth,'from':list(geo.CENTER),'to':[float('nan'),58]}).status_code,400)
    def test_migration_handles_existing_current_metadata(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        migration=importlib.import_module('migrations.versions.0053_driver_app_commands')
        with self.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):migration.upgrade()
        # Simulate an upgrade from v179 where the new table does not exist.
        with self.engine.begin() as conn:
            DriverAppCommand.__table__.drop(conn)
            with Operations.context(MigrationContext.configure(conn)):migration.upgrade()

if __name__=='__main__':unittest.main()
