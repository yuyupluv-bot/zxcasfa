"""V173: compact passenger messages, fare recovery and shared-VPN login."""
from __future__ import annotations
import ast
import datetime as dt
import json
import math
import re
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def source(file):
    return (ROOT / file).read_text('utf-8')


def node(file, name):
    return next(n for n in ast.walk(ast.parse(source(file)))
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)


def load(file, name, scope=None):
    scope = {} if scope is None else scope
    # Route decorators are irrelevant in the standalone runtime harness.
    n = node(file, name)
    if isinstance(n, ast.FunctionDef):
        n.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), n], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), file, 'exec'), scope)
    return scope[name]


@contextmanager
def relative_modules(**modules):
    package = types.ModuleType('v173_runtime')
    package.__path__ = []
    items = {'v173_runtime': package}
    for name, value in modules.items():
        setattr(package, name, value)
        items['v173_runtime.' + name] = value
    with patch.dict(sys.modules, items):
        yield


class MessageChangesV173(unittest.TestCase):
    def test_exact_queue_texts_in_every_dispatch_path(self):
        for file in ('bot/order_service.py', 'bot/passenger_queue.py'):
            text = source(file)
            self.assertIn('⏰ Минутку…', text)
            self.assertIn('⏰ Чуть подождать, первый свободный, возьмет вашу заявку…', text)
            self.assertNotIn('Минутку, передаем заявку свободному водителю.', text)
            self.assertNotIn('Предлагаем водителям взять вашу заявку как параллельную.', text)

    def test_no_new_acceptance_notice(self):
        vk = Mock()
        load('bot/order_service.py', 'notify_driver_found', {'vk': vk})(None, NS(offer_notice_outbox_id=None))
        self.assertEqual(vk.mock_calls, [])
        self.assertNotIn('Водитель взял вашу заявку и указывает время прибытия', source('bot/order_service.py'))

    def test_legacy_acceptance_notice_is_retired(self):
        order = NS(offer_notice_outbox_id=7)
        outbox = types.ModuleType('v173_runtime.outbox_service')
        outbox.cancel_or_delete = Mock(return_value=True)
        with relative_modules(outbox_service=outbox):
            load('bot/order_service.py', 'notify_driver_found', {'__package__': 'v173_runtime'})(None, order)
        outbox.cancel_or_delete.assert_called_once_with(None, 7)
        self.assertIsNone(order.offer_notice_outbox_id)

    def test_compact_card_exact_example(self):
        scope = {}
        for name in ('_passenger_review_word', 'driver_rating_text', 'departure_driver_card_text'):
            load('bot/order_service.py', name, scope)
        card = scope['departure_driver_card_text']
        driver = NS(vk_id=141670738, full_name='Владислав Данилов', rating=5.0,
                    rating_count=80, car_model='Москвич 3', car_color='Серый', car_number='893')
        self.assertEqual(card(driver), '[id141670738|Владислав Данилов] 5.0 ⭐ (80 отзывов)\nАвто: Москвич 3, Серый, 893')
        driver.rating_count = 0
        self.assertIn('] — ⭐ (нет отзывов)\n', card(driver))

    def eta(self, confirmed=False, partner=False, dispatcher=False, delivery=False):
        driver = NS(id=1, vk_id=11)
        passenger = NS(id=2, vk_id=22)
        order = NS(id=7, number=1067, driver_id=1, passenger_id=2,
                   dispatcher_id=2 if dispatcher else None, departure_prompt_outbox_id=None,
                   driver_departed_at=None, actuality_confirmed=confirmed)
        session = Mock()
        session.get.side_effect = lambda model, id: driver if id == 1 else passenger
        vk = Mock()
        vk.send_tracked_message.return_value = 99
        service = Mock()
        service.multi_car_label.return_value = None
        service.departure_driver_card_text.return_value = '[id11|Водитель] 5.0\nАвто: Москвич 3, Серый, 893'
        service.dispatcher_driver_details.return_value = 'Данные водителя'
        scope = {'User': object, 'log': Mock(), 'show_main_menu': Mock(), 'order_service': service,
                 'time_utils': NS(now=lambda: dt.datetime.now(dt.timezone.utc)),
                 'delivery_service': NS(is_delivery=lambda o: delivery, is_partner_delivery=lambda o: partner),
                 '_is_dispatcher_order': lambda o: dispatcher, '_driver_card': lambda u: 'Полная карточка',
                 'vk': vk, 'kb': Mock(), 'set_state': Mock(),
                 'States': NS(P_IN_RIDE='passenger_in_ride', D_IN_RIDE='driver_in_ride'),
                 '_driver_ride_kb': Mock(return_value='ride')}
        load('bot/handlers.py', '_apply_eta', scope)(session, driver, order, 2)
        return order, vk, service

    def test_eta_question_and_driver_are_in_one_card_v181(self):
        order, vk, _ = self.eta()
        self.assertEqual(vk.send_tracked_message.call_args.args[:2],
                         (22, '[id11|Водитель] 5.0\nАвто: Москвич 3, Серый, 893\n⏰ Будет через 2 мин.\nЖдёте машинку?'))
        self.assertFalse(any(c.args[0] == 22 for c in vk.send_message.call_args_list))
        self.assertEqual(order.departure_prompt_outbox_id, 99)

    def test_confirmed_eta_does_not_repeat_question(self):
        _, vk, _ = self.eta(confirmed=True)
        vk.send_tracked_message.assert_not_called()
        self.assertEqual(vk.send_message.call_args_list[0].args[1],
                         '[id11|Водитель] 5.0\nАвто: Москвич 3, Серый, 893\n⏰ Будет через 2 мин.')

    def test_partner_eta_keeps_pickup_semantics(self):
        order, vk, service = self.eta(partner=True)
        service.departure_driver_card_text.assert_not_called()
        vk.send_tracked_message.assert_not_called()
        self.assertIn('Водитель заберёт заказ примерно через 2 мин.', vk.send_message.call_args_list[0].args[1])
        self.assertTrue(order.actuality_confirmed)

    def test_dispatcher_eta_stays_dispatcher_only(self):
        _, vk, service = self.eta(dispatcher=True)
        service.dispatcher_driver_details.assert_called_once()
        vk.send_tracked_message.assert_not_called()
        self.assertIn('По заявке #1067', vk.send_message.call_args_list[0].args[1])

    def test_yes_has_no_passenger_ack_and_keeps_ride_controls(self):
        order = NS(id=7, status='assigned', driver_id=1, departure_prompt_outbox_id=99)
        session = Mock()
        driver = NS(vk_id=11)
        session.get.side_effect = [driver, NS(text='Карточка\n⏰ Будет через 2 мин.\nЖдете?'), driver]
        outbox = types.ModuleType('v173_runtime.outbox_service')
        outbox.finalize_tracked_message = Mock(return_value=True)
        scope = {'__package__': 'v173_runtime', 're': re, 'User': object, 'OutboxMessage': object,
                 'passenger_order_for': Mock(return_value=order), 'show_main_menu': Mock(),
                 'passenger_cancel_request': Mock(), 'vk': Mock(), 'kb': Mock(),
                 'set_state': Mock(), 'States': NS(P_IN_RIDE='ride')}
        user = NS(vk_id=22)
        with relative_modules(outbox_service=outbox):
            callback = load('bot/handlers.py', 'passenger_departure_response', scope)
            callback(session, user, False, 7)
            callback(session, user, False, 7)
        scope['vk'].send_message.assert_called_once_with(11, 'Клиент ожидает вас на месте')
        args = outbox.finalize_tracked_message.call_args
        self.assertEqual(args.args[2], 'Карточка\n⏰ Будет через 2 мин.')
        scope['vk'].send_tracked_message.assert_called_once_with(22, 'Машинка в пути')
        self.assertTrue(args.kwargs['defer_edit'])
        self.assertTrue(args.kwargs['replace_text'])
        scope['kb'].passenger_ride_keyboard.assert_not_called()
        self.assertEqual(args.kwargs['replacement_keyboard'], '{"buttons":[],"inline":true}')
        self.assertTrue(order.actuality_confirmed)
        self.assertIsNone(order.departure_prompt_outbox_id)

    def test_passenger_ride_controls_can_be_inline_with_exact_order(self):
        scope = {'json': json, 'math': math}
        tree = ast.parse(source('bot/keyboards.py'))
        constants = [n for n in tree.body if isinstance(n, ast.Assign)
                     and isinstance(n.value, ast.Constant)]
        exec(compile(ast.Module(body=constants, type_ignores=[]), '<keyboard constants>', 'exec'), scope)
        for name in ('fit_label', '_btn', 'keyboard', 'passenger_ride_keyboard'):
            load('bot/keyboards.py', name, scope)
        result = json.loads(scope['passenger_ride_keyboard'](7, inline=True))
        self.assertTrue(result['inline'])
        payloads = [json.loads(b['action']['payload']) for row in result['buttons'] for b in row]
        self.assertEqual({p['order_id'] for p in payloads}, {7})
        self.assertEqual({p['cmd'] for p in payloads}, {'driver_wait_remaining', 'chat', 'cancel_ride'})

    def test_payment_message_exact_and_repeated_send_blocked(self):
        order = NS(id=7, number=1067, passenger_id=2, status='in_progress', payment_details_sent=False)
        user = NS(vk_id=11, show_payment_details=True, payment_type='phone', payment_phone='89963246055',
                  payment_bank='Ozon', payment_card=None, payment_recipient='Александр Валерьевич')
        session = Mock(); session.get.return_value = NS(vk_id=22)
        vk = Mock(); vk.send_message.return_value = True
        scope = {'active_order_for': Mock(return_value=order), 'show_main_menu': Mock(),
                 'delivery_service': NS(is_partner_delivery=lambda o: False), '_payment_details_ready': lambda u: True,
                 '_driver_ride_kb': Mock(return_value='ride'), '_is_dispatcher_order': lambda o: False,
                 'User': object, 'vk': vk, 'kb': Mock(), 'order_service': NS(multi_car_label=lambda s, o: None),
                 'waiting_service': NS(snapshot=lambda s, o: (0, 0)), 're': re}
        scope['_payment_details_text'] = load('bot/handlers.py', '_payment_details_text', {'re': re})
        callback = load('bot/handlers.py', 'driver_send_payment_details', scope)
        callback(session, user)
        expected = 'Реквизиты для оплаты по заявке #1067\nтелефон 89963246055, банк Ozon, получатель Александр Валерьевич'
        self.assertEqual(vk.send_message.call_args_list[0].args, (22, expected))
        self.assertTrue(order.payment_details_sent)
        callback(session, user)
        self.assertEqual(sum(c.args[0] == 22 for c in vk.send_message.call_args_list), 1)


class FinishRecoveryV173(unittest.TestCase):
    def test_payload_accepts_json_and_decoded_dict(self):
        parse = load('bot/handlers.py', 'parse_payload', {'json': json})
        for raw in ({'cmd': 'finish'}, '{"cmd":"finish"}'):
            self.assertEqual(parse(raw), {'cmd': 'finish'})
        for raw in (None, '', '[]', '[1]', 'null', '3', '{bad', []):
            self.assertEqual(parse(raw), {})

    def test_exact_finish_labels_only(self):
        recover = load('bot/handlers.py', 'driver_trip_command_from_label', {'re': re})
        for text in ('🏁 Завершить', 'Завершить', '🏁 Завершить доставку', ' 🏁\ufe0f Завершить '):
            self.assertEqual(recover(text), 'finish')
        for text in ('Как завершить?', 'Не завершить', 'Ленина 7 завершить', '350', 'Завершить заявку #7'):
            self.assertIsNone(recover(text))

    def route(self, role='driver', raw=None, text='🏁 Завершить', peer_id=11, status='in_progress', is_driver=True):
        user = NS(id=1, vk_id=11, full_name='Водитель', driver_gender='male', is_blocked=False,
                  role=role, has_role=lambda r: is_driver if r == 'driver' else r == role)
        order = NS(id=7, status=status)
        session = Mock(); session.query.return_value.filter.return_value.one_or_none.return_value = user
        states = load('bot/states_service.py', 'States')
        scope = {'json': json, 're': re, 'config': NS(ADMIN_VK_IDS=[]),
                 'ROLE_DRIVER': 'driver', 'ROLE_ADMIN': 'admin', 'ROLE_PASSENGER': 'passenger', 'ROLE_DISPATCHER': 'dispatcher',
                 'User': NS(vk_id=0), '_cached_blocked_vk_ids': lambda s: set(),
                 'offered_order_for': lambda s, u: None, 'active_order_for': lambda s, u, as_driver=False: order,
                 'abuse_service': NS(allow_event=lambda *a: True), 'vk': NS(message_attachments=lambda m: []),
                 'verification': NS(verify_user=lambda *a: (True, None)),
                 'fake_calls_service': NS(is_passenger_blocked=lambda u: False),
                 'get_state': lambda *a: NS(state=states.D_IN_RIDE), 'States': states,
                 '_DRIVER_OWNED_INPUT_STATES': {states.D_FINISH_PRICE},
                 'handle_driver': Mock(return_value='driver'), 'handle_dispatcher': Mock(),
                 'handle_admin': Mock(), 'handle_passenger': Mock(), 'log': Mock()}
        scope['parse_payload'] = load('bot/handlers.py', 'parse_payload', {'json': json})
        scope['driver_trip_command_from_label'] = load('bot/handlers.py', 'driver_trip_command_from_label', {'re': re})
        event = NS(obj=NS(message={'from_id': 11, 'peer_id': peer_id, 'text': text, 'payload': raw}))
        load('bot/handlers.py', '_handle_message_impl', scope)(session, event)
        return scope

    def test_payloadless_finish_routes_to_driver_for_all_selected_roles(self):
        for role in ('driver', 'dispatcher', 'admin', 'passenger'):
            with self.subTest(role=role):
                scope = self.route(role=role)
                self.assertEqual(scope['handle_driver'].call_args.args[4], {'cmd': 'finish'})

    def test_dict_finish_routes_even_when_menu_role_is_dispatcher(self):
        scope = self.route(role='dispatcher', raw={'cmd': 'finish'})
        self.assertEqual(scope['handle_driver'].call_args.args[4], {'cmd': 'finish'})

    def test_group_text_never_finishes_private_ride(self):
        scope = self.route(peer_id=2_000_000_001)
        scope['handle_driver'].assert_not_called()

    def test_finish_label_before_boarding_is_not_recovered(self):
        scope = self.route(role='dispatcher', status='assigned')
        scope['handle_driver'].assert_not_called()

    def test_passenger_text_is_not_driver_command(self):
        scope = self.route(role='passenger', is_driver=False)
        scope['handle_driver'].assert_not_called()
        self.assertEqual(scope['handle_passenger'].call_args.args[4], {})

    def test_finish_prompt_sets_state_before_enqueue(self):
        for dispatcher in (None, 2):
            order = NS(id=7, dispatcher_id=dispatcher, night_surcharge=True)
            calls = []
            scope = {'active_order_for': lambda *a, **kw: order, 'show_main_menu': Mock(),
                     'delivery_service': NS(is_delivery=lambda o: False),
                     'waiting_service': NS(snapshot=lambda *a: (5, 30)), 'night_tariff': NS(amount=lambda s: 50),
                     'set_state': lambda *a, **kw: calls.append(('state', a, kw)),
                     'vk': NS(send_message=lambda *a, **kw: calls.append(('send', a, kw))),
                     '_active_driver_order_keyboard': lambda *a: 'ride', 'States': NS(D_FINISH_PRICE='fare')}
            load('bot/handlers.py', 'driver_finish_prompt', scope)(None, NS(vk_id=11))
            self.assertEqual([x[0] for x in calls], ['state', 'send'])
            self.assertEqual(calls[0][1][2], 'fare')
            self.assertFalse(calls[0][2]['merge'])
            self.assertIn('Введите итоговую стоимость поездки одним числом (₽):', calls[1][1][1])
            self.assertIn('30 ₽', calls[1][1][1])
            self.assertEqual(calls[1][2]['keyboard'], 'ride')

    def test_finish_handler_routes_button_and_plain_amount_separately(self):
        states = load('bot/states_service.py', 'States')
        scope = {'States': states, 'offered_order_for': lambda *a: None,
                 'active_order_for': lambda *a, **kw: NS(id=7),
                 'temporary_driver_service': NS(expire_driver_if_due=lambda *a: False),
                 '_DRIVER_MENU_CMDS': set(), 'driver_finish_prompt': Mock(), 'driver_complete_ride': Mock()}
        handler = load('bot/handlers.py', 'handle_driver', scope)
        handler(None, NS(vk_id=11), states.D_IN_RIDE, '🏁 Завершить', {'cmd': 'finish'}, [])
        scope['driver_finish_prompt'].assert_called_once()
        scope['driver_complete_ride'].assert_not_called()
        handler(None, NS(vk_id=11), states.D_FINISH_PRICE, '350', {}, [])
        scope['driver_complete_ride'].assert_called_once_with(None, unittest.mock.ANY, '350')

    def test_manual_fare_completes_ride_without_adding_surcharges(self):
        for dispatcher in (None, 2):
            with self.subTest(dispatcher=dispatcher):
                order = NS(id=7, number=1067, dispatcher_id=dispatcher, passenger_id=2,
                           night_surcharge=True, status='in_progress', payment_details_sent=False)
                session = Mock(); session.get.return_value = NS(vk_id=22)
                parallel = types.ModuleType('v173_runtime.parallel_orders')
                parallel.release_route_offers_for_trip = Mock(); parallel.has_pending = Mock(return_value=False)
                rating = types.ModuleType('v173_runtime.rating_expiry_service'); rating.schedule_expiry = Mock()
                scope = {'__package__': 'v173_runtime', 're': re, 'math': math,
                         'active_order_for': lambda *a, **kw: order, 'show_main_menu': Mock(), 'User': object,
                         'waiting_service': NS(finalize=lambda *a: (5, 30)), 'night_tariff': NS(amount=lambda s: 50),
                         'booking_service': Mock(), 'time_utils': NS(now=lambda: dt.datetime.now(dt.timezone.utc)),
                         'config': NS(DISPATCHER_COMMISSION=0.1), 'DispatcherCommission': lambda **kw: NS(**kw),
                         'timers': Mock(), '_leave_temporary_chat_line': Mock(), '_ask_driver_rate_passenger': Mock(),
                         'lines': Mock(), 'vk': Mock(), '_is_dispatcher_order': lambda o: dispatcher is not None,
                         'delivery_service': NS(is_partner_delivery=lambda o: False),
                         'bm': NS(render=lambda *a, **kw: ('Оцените поездку', None)), 'kb': Mock(),
                         'reset': Mock(), 'States': NS(MAIN_MENU='menu'), 'PRICE_MIN': 100, 'PRICE_MAX': 50_000, 'log': Mock()}
                for name in ('_parse_price', '_price_in_range', '_price_range_message'):
                    load('bot/handlers.py', name, scope)
                scope['vk'].send_tracked_message.return_value = 99
                with relative_modules(parallel_orders=parallel, rating_expiry_service=rating):
                    load('bot/handlers.py', 'driver_complete_ride', scope)(session, NS(id=1, vk_id=11), '350 ₽')
                self.assertEqual(order.status, 'completed')
                self.assertEqual(order.price, 350)
                self.assertEqual(order.waiting_fee, 30)
                self.assertIsNotNone(order.completed_at)
                if dispatcher:
                    self.assertEqual(session.add.call_args.args[0].amount, 35)
                else:
                    rating.schedule_expiry.assert_called_once_with(7, driver_side=False)

    def test_fare_prompt_has_critical_outbox_priority(self):
        cls = node('bot/vk_client.py', 'VKClient')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_message_priority')
        method.decorator_list = []
        scope = {'re': re}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<priority>', 'exec'), scope)
        priority = scope['_message_priority']('Введите итоговую стоимость поездки одним числом (₽):')
        self.assertGreaterEqual(priority, 5000)

class InlineFinalizationV173(unittest.TestCase):
    def finalize(self, status, replacement=None):
        row = NS(id=99, peer_id=22, status=status, keyboard='{"inline":true,"buttons":[]}',
                 text='Карточка\nЖдете?', attachment='audio_message1_2', last_error='vk_message_id:777')
        session = Mock(); session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = row
        vk = types.ModuleType('v173_runtime.vk_client'); vk.vk = Mock(); vk.vk.edit_message.return_value = True
        scope = {'__package__': 'v173_runtime', 'OutboxMessage': NS(id=0),
                 'time_utils': NS(now=lambda: dt.datetime.now(dt.timezone.utc)),
                 'has_usable_vk_id': lambda r: status == 'sent', '_stored_vk_message_id': lambda r: 777,
                 '_stored_vk_cmid': lambda r: None}
        with relative_modules(vk_client=vk):
            result = load('bot/outbox_service.py', 'finalize_tracked_message', scope)(
                session, 99, 'Карточка', replace_text=True, replacement_keyboard=replacement)
        self.assertTrue(result)
        self.assertEqual(row.text, 'Карточка')
        self.assertEqual(row.attachment, 'audio_message1_2')
        return row, vk.vk

    def test_followup_keyboard_survives_pending_sending_and_sent_cards(self):
        for status in ('pending', 'failed', 'sending', 'sent'):
            with self.subTest(status=status):
                keyboard = '{"inline":true,"buttons":[[{"action":{"type":"text","label":"Связаться"}}]]}'
                row, vk = self.finalize(status, keyboard)
                self.assertEqual(row.keyboard, keyboard)
                if status == 'sending':
                    self.assertEqual(row.status, 'finalize_requested')
                if status == 'sent':
                    self.assertEqual(vk.edit_message.call_args.args[3], keyboard)

    def test_existing_finalize_callers_still_clear_inline_buttons(self):
        row, vk = self.finalize('sent')
        self.assertEqual(json.loads(row.keyboard)['buttons'], [])
        self.assertTrue(json.loads(row.keyboard)['inline'])


class VpnLoginV173(unittest.TestCase):
    """Execute the login handler against a lightweight SQLite query adapter."""
    def setUp(self):
        import sqlite3
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE login_attempts (id INTEGER PRIMARY KEY, ip_address TEXT, login TEXT, success INTEGER, created_at TEXT)')
        self.now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
        self.session = {}
        self.request = NS(method='POST', form={'login': 'admin', 'password': 'correct'},
                          headers={'X-Forwarded-For': '203.0.113.20'}, remote_addr='172.18.0.5')
        self.verify_calls = []
        connection, now = self.connection, self.now

        class Field:
            def __init__(self, name): self.name = name
            def __eq__(self, value): return (self.name + ' = ?', [value])
            def __ge__(self, value): return (self.name + ' >= ?', [value.isoformat()])
            def is_(self, value): return (self.name + ' = ?', [int(value)])

        class Attempt:
            id = Field('id'); ip_address = Field('ip_address'); login = Field('login')
            success = Field('success'); created_at = Field('created_at')
            def __init__(self, **kwargs): self.values = kwargs

        class Query:
            def filter(self, *conditions): self.conditions = conditions; return self
            def scalar(self):
                sql = 'SELECT COUNT(*) FROM login_attempts WHERE ' + ' AND '.join(c[0] for c in self.conditions)
                params = [v for c in self.conditions for v in c[1]]
                return connection.execute(sql, params).fetchone()[0]

        class DB:
            def query(self, *args): return Query()
            def add(self, row):
                v = row.values
                connection.execute('INSERT INTO login_attempts (ip_address,login,success,created_at) VALUES (?,?,?,?)',
                                   (v['ip_address'], v['login'], int(v['success']), now.isoformat()))
            def commit(self): connection.commit()

        def verify(db, login, password):
            self.verify_calls.append((login, password))
            return NS(id=1, login=login) if login == 'admin' and password == 'correct' else None

        self.scope = {'request': self.request, 'dt': dt, 'time_utils': NS(now=lambda: self.now),
                      'db': lambda: DB(), 'func': NS(count=lambda value: value), 'LoginAttempt': Attempt,
                      'ProgrammingError': type('ProgrammingError', (Exception,), {}),
                      'verify_admin': verify, 'session': self.session, 'log_action': Mock(),
                      'render_template': lambda name, **kwargs: 'blocked' if kwargs.get('blocked') else 'login',
                      'redirect': lambda url: ('redirect', 302), 'url_for': lambda name: '/' + name,
                      'flash': Mock()}
        self.login = load('web/app.py', 'login', self.scope)

    def tearDown(self): self.connection.close()

    def failure(self, ip, login):
        self.connection.execute('INSERT INTO login_attempts(ip_address,login,success,created_at) VALUES(?,?,0,?)',
                                (ip, login, self.now.isoformat()))
        self.connection.commit()

    def test_unrelated_failures_on_shared_vpn_ip_do_not_block_login(self):
        for i in range(8): self.failure('203.0.113.20', 'other' + str(i))
        self.assertEqual(self.login(), ('redirect', 302))
        self.assertEqual(self.session['admin_id'], 1)

    def test_same_account_is_limited_across_vpn_ip_changes(self):
        for i in range(5): self.failure('203.0.113.' + str(i), 'admin')
        self.assertEqual(self.login(), ('blocked', 429))
        self.assertNotIn('admin_id', self.session)
        self.assertEqual(self.verify_calls, [])

    def test_wrong_password_does_not_create_authenticated_session(self):
        self.request.form['password'] = 'wrong'
        self.assertEqual(self.login(), 'login')
        self.assertNotIn('admin_id', self.session)
        self.assertEqual(self.connection.execute('SELECT success FROM login_attempts').fetchone()[0], 0)

    def test_session_survives_vpn_endpoint_change(self):
        from functools import wraps
        self.login()
        guard = load('web/auth.py', 'login_required', {'wraps': wraps, 'session': self.session,
                    'redirect': self.scope['redirect'], 'url_for': self.scope['url_for']})
        protected = guard(lambda: 'dashboard')
        self.request.remote_addr = '198.51.100.30'
        self.request.headers['X-Forwarded-For'] = '198.51.100.30'
        self.assertEqual(protected(), 'dashboard')

    def test_csrf_secure_cookies_and_password_checks_remain(self):
        text = source('web/app.py')
        self.assertIn('SESSION_COOKIE_SECURE=', text)
        self.assertIn('Недействительный CSRF-токен', text)
        self.assertIn('verify_admin(db(), login_value, request.form["password"])', text)
        self.assertIn('if failures >= 5:', text)
        login = ast.get_source_segment(text, node('web/app.py', 'login'))
        self.assertNotIn('LoginAttempt.ip_address == ip', login)
