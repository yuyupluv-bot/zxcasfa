"""Local deployment integration test. Run ONLY against a disposable PostgreSQL.

BOTHOST_TEST_DATABASE=1 DATABASE_URL=... python tests/integration_bothost.py
No real VK/FCM requests are made. Creates/deletes synthetic test rows.
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.getenv('BOTHOST_TEST_DATABASE') != '1':
    raise SystemExit('Use only a disposable DB and explicitly set BOTHOST_TEST_DATABASE=1')
import datetime as dt
import hashlib
import secrets
from unittest.mock import patch
from sqlalchemy import inspect
from common.database import engine, session_scope
from common.models import User, DriverPushDevice, DriverAppPairCode, PushScenario
from common import time_utils
from bot import push_service
from web.app import app

checks = 0

def check(condition, label):
    global checks
    assert condition, label
    checks += 1
    print('PASS:', label)

c = app.test_client()
base = 'https://localhost'
for route in ('/live', '/health', '/healthz', '/login', '/privacy', '/driver-app/download.apk'):
    check(c.get(route, base_url=base).status_code == 200, route)
check(c.get('/', base_url=base).status_code == 302, 'unauthenticated dashboard redirects')
check(c.post('/login', data={'login':'testadmin','password':'wrong'}, base_url=base).status_code == 400, 'CSRF enforced')
c.get('/login', base_url=base)
with c.session_transaction() as session:
    csrf = session['csrf_token']
check(c.post('/login', data={'login':os.environ['ADMIN_LOGIN'], 'password':os.environ['ADMIN_PASSWORD'], 'csrf_token':csrf}, base_url=base).status_code == 302, 'admin login succeeds')
check(c.get('/', base_url=base).status_code == 200, 'authenticated dashboard')
check(c.get('/driver-app', base_url=base).status_code == 200, 'notification settings')
check('device_secret_hash' in {x['name'] for x in inspect(engine).get_columns('driver_push_devices')}, 'Firebase device migration')

code = '123456'
suffix = secrets.randbelow(1000000)
with session_scope() as s:
    a = User(vk_id=9000000000+suffix, full_name='LOCAL TEST DRIVER', role='driver', granted_roles='driver,passenger')
    b = User(vk_id=9100000000+suffix, full_name='LOCAL TEST OTHER', role='driver', granted_roles='driver,passenger')
    s.add_all([a,b]); s.flush(); driver_id, other_id = a.id, b.id
    s.add(DriverAppPairCode(driver_id=driver_id, code_hash=hashlib.sha256(code.encode()).hexdigest(), expires_at=time_utils.now()+dt.timedelta(minutes=5)))
    s.add(DriverPushDevice(driver_id=other_id, token='local-other-'+str(suffix), is_active=True))
r = c.post('/api/driver-app/register', json={'code':code, 'token':'local-token-'+str(suffix)}, base_url=base)
check(r.status_code == 200, 'pairing without web-admin CSRF')
device = r.get_json()
check(bool(device.get('device_secret')), 'per-device secret issued')
check(c.post('/api/driver-app/register', json={'code':code, 'token':'duplicate'}, base_url=base).status_code == 403, 'one-time pairing cannot be reused')
check(c.post('/api/driver-app/token', json={'device_id':device['device_id'], 'device_secret':'wrong','token':'x'}, base_url=base).status_code == 403, 'token rotation rejects wrong secret')
new_token = 'local-rotated-'+str(suffix)
auth = dict(device_id=device['device_id'], device_secret=device['device_secret'])
check(c.post('/api/driver-app/token', json={**auth,'token':new_token}, base_url=base).status_code == 200, 'authenticated FCM token rotation')
with session_scope() as s:
    with patch.object(push_service, '_firebase', return_value=True), patch('firebase_admin.messaging.send', return_value='mock-message') as sender:
        check(push_service.send(s, driver_id, 'new_order') == 1, 'push accepted by mocked Firebase')
        check(sender.call_count == 1 and sender.call_args.args[0].token == new_token, 'push only goes to selected driver')
        check(sender.call_args.args[0].android.priority == 'high', 'high-priority Android data message')
    class FakeUnregistered(Exception): pass
    with patch.object(push_service, '_firebase', return_value=True), patch('firebase_admin.messaging.UnregisteredError', FakeUnregistered), patch('firebase_admin.messaging.send', side_effect=FakeUnregistered()):
        check(push_service.send(s, driver_id, 'new_order') == 0, 'invalid FCM registration handled')
        check(s.get(DriverPushDevice, device['device_id']).is_active is False, 'invalid token deactivated')
check(c.post('/api/driver-app/unregister', json=auth, base_url=base).status_code == 200, 'app disconnect')
with session_scope() as s:
    s.query(DriverPushDevice).filter(DriverPushDevice.driver_id.in_([driver_id,other_id])).delete(synchronize_session=False)
    s.query(DriverAppPairCode).filter_by(driver_id=driver_id).delete(synchronize_session=False)
    s.query(User).filter(User.id.in_([driver_id,other_id])).delete(synchronize_session=False)
print(f'{checks} integration checks passed (local PostgreSQL; FCM mocked; no real delivery).')
