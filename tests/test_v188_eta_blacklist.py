"""V188 offline tests: execute original functions, mock network/ORM boundaries.

Run: python -m unittest discover -s tests -p test_v188_eta_blacklist.py -v
These checks do not claim real PostgreSQL or VK delivery coverage.
"""
import ast
import datetime as dt
import json
import math
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load, source

NOW = dt.datetime(2026, 11, 24, 15, 0, tzinfo=dt.timezone.utc)


def keyboards():
    scope = {"json": json, "math": math}
    tree = ast.parse(source('bot/keyboards.py'))
    nodes = [n for n in tree.body if isinstance(n, (ast.Assign, ast.FunctionDef))]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes
    ], type_ignores=[])), '<keyboards>', 'exec'), scope)
    return NS(**scope)


def actions(raw):
    return [(button['action']['label'], json.loads(button['action']['payload']))
            for row in json.loads(raw)['buttons'] for button in row]


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.kb = keyboards()

    def test_offer_shows_six_eta_options_custom_then_decline(self):
        raw = self.kb.order_offer_keyboard(7)
        data = actions(raw)
        self.assertTrue(json.loads(raw)['inline'])
        self.assertEqual([p['minutes'] for _, p in data if p['cmd'] == 'offer_eta'], [1, 3, 5, 10, 15, 25])
        self.assertEqual([p['cmd'] for _, p in data[-2:]], ['offer_eta_custom', 'decline'])
        self.assertTrue(all(p['order_id'] == 7 for _, p in data))
        self.assertNotIn('accept', [p['cmd'] for _, p in data])
        self.assertNotIn('Поехать', raw)

    def test_customer_price_offer_uses_same_eta_choices(self):
        self.assertEqual(self.kb.order_offer_keyboard(7, True), self.kb.order_offer_keyboard(7, False))

    def test_online_menu_matches_main_info_entry(self):
        data = actions(self.kb.driver_menu(True))
        cmds = [p['cmd'] for _, p in data]
        self.assertIn('driver_info', cmds)
        self.assertNotIn('earnings', cmds)
        self.assertNotIn('reviews', cmds)
        self.assertIn('earnings', [p['cmd'] for _, p in actions(self.kb.driver_info_menu())])
        self.assertIn('reviews', [p['cmd'] for _, p in actions(self.kb.driver_info_menu())])
        self.assertIn('blacklist', [p['cmd'] for _, p in actions(self.kb.driver_info_menu())])

    def test_blacklist_main_keyboard_is_persistent(self):
        raw = self.kb.blacklist_menu()
        self.assertFalse(json.loads(raw).get('inline', False))
        self.assertEqual([p['cmd'] for _, p in actions(raw)], ['blacklist_add', 'blacklist_remove_menu', 'driver_info'])

    def test_remove_menu_is_inline_and_each_button_has_its_client(self):
        raw = self.kb.blacklist_remove_keyboard([(201, 'Клиент'), (202, None)], 2, 3)
        self.assertTrue(json.loads(raw)['inline'])
        self.assertEqual([p['vk_id'] for _, p in actions(raw) if p['cmd'] == 'blacklist_remove'], [201, 202])
        self.assertEqual([p['page'] for _, p in actions(raw) if p['cmd'] == 'blacklist_remove_menu'], [1, 3])

    def test_rating_blacklist_is_under_one_star_and_above_skip(self):
        data = actions(self.kb.passenger_rating_keyboard(7))
        self.assertEqual(data[-3][1]['stars'], 1)
        self.assertEqual(data[-2][1], {'cmd': 'blacklist_rating', 'order_id': 7})
        self.assertEqual(data[-1][1]['cmd'], 'skip_rate_passenger')

    def test_legacy_departure_keyboard_has_no_depart_button(self):
        data = actions(self.kb.driver_depart_keyboard(7))
        self.assertNotIn('chat_depart', [p['cmd'] for _, p in data])
        self.assertIn('eta_pick', [p['cmd'] for _, p in data])

    def test_all_new_offer_commands_pass_both_offer_locks(self):
        tree = ast.parse(source('bot/handlers.py'))
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == '_DRIVER_OFFER_ALLOWED_CMDS' for t in n.targets))
        allowed = ast.literal_eval(assignment.value)
        for _, payload in actions(self.kb.order_offer_keyboard(7)):
            self.assertIn(payload['cmd'], allowed)


class AgeAndDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.scope = dict(dt=dt, time_utils=NS(now=lambda: NOW), log=Mock(), ScheduledJob=NS(job_key=0, status=0))
        self.needs = load('bot/handlers.py', 'claim_needs_actuality', self.scope)
        self.expired = load('bot/handlers.py', 'offer_deadline_passed', self.scope)

    def test_age_strict_boundary(self):
        for seconds, expected in [(0, False), (59, False), (60, False), (119, False), (120, False), (121, True), (900, True)]:
            with self.subTest(seconds=seconds):
                order = NS(created_at=NOW-dt.timedelta(seconds=seconds), driver_accept_time=NOW)
                self.assertEqual(self.needs(order), expected)

    def test_age_measured_at_acceptance_not_delayed_passenger_answer(self):
        order = NS(created_at=NOW-dt.timedelta(minutes=10), driver_accept_time=NOW-dt.timedelta(minutes=9))
        self.assertFalse(self.needs(order))

    def test_naive_utc_timestamp_uses_same_boundary(self):
        self.assertTrue(self.needs(NS(created_at=(NOW-dt.timedelta(seconds=121)).replace(tzinfo=None), driver_accept_time=NOW)))

    def test_missing_creation_time_never_prompts(self):
        self.assertFalse(self.needs(NS()))

    def test_future_timestamp_does_not_prompt(self):
        self.assertFalse(self.needs(NS(created_at=NOW+dt.timedelta(seconds=10), driver_accept_time=NOW)))

    def test_deadline_strictly_rejects_at_sixty(self):
        for offset, expected in [(1, False), (0, True), (-1, True)]:
            with self.subTest(offset=offset):
                self.assertEqual(self.expired(Mock(), NS(eta_offer_expires_at=NOW+dt.timedelta(seconds=offset))), expected)

    def test_legacy_job_is_used_when_explicit_deadline_absent(self):
        session = Mock()
        session.query.return_value.filter.return_value.one_or_none.return_value = NS(run_at=NOW-dt.timedelta(seconds=1))
        self.assertTrue(self.expired(session, NS(id=7)))

    def test_missing_timer_is_not_infinite_acceptance(self):
        session = Mock()
        session.query.return_value.filter.return_value.one_or_none.return_value = None
        self.assertTrue(self.expired(session, NS(id=7)))

    def test_local_timezone_is_same_instant(self):
        local = dt.timezone(dt.timedelta(hours=5))
        order = NS(created_at=(NOW-dt.timedelta(seconds=121)).astimezone(local), driver_accept_time=NOW)
        self.assertTrue(self.needs(order))


class OfferInputTests(unittest.TestCase):
    def setUp(self):
        self.user = NS(id=1, vk_id=101)
        self.order = NS(id=7, customer_price=None)
        self.scope = dict(re=re, vk=Mock(), _live_eta_offer=Mock(return_value=self.order),
                          driver_accept=Mock(), set_state=Mock(), States=NS(D_OFFER_ETA='offer_eta'))
        self.accept = load('bot/handlers.py', 'accept_offer_eta', self.scope)
        self.custom = load('bot/handlers.py', 'offer_eta_custom', self.scope)

    def test_valid_eta_accepts_once_with_eta_and_no_early_passenger_notice(self):
        self.accept(None, self.user, 7, 10)
        self.scope['driver_accept'].assert_called_once_with(None, self.user, 7, notify_passenger=False, selected_eta=10)

    def test_custom_minute_text_is_accepted(self):
        self.accept(None, self.user, 7, '12 мин.')
        self.assertEqual(self.scope['driver_accept'].call_args.kwargs['selected_eta'], 12)

    def test_invalid_values_never_accept(self):
        for value in (None, '', 'abc', 0, -1, '1.5', '1 5', '10abc', 1441, 10000):
            with self.subTest(value=value):
                self.scope['driver_accept'].reset_mock()
                self.accept(None, self.user, 7, value)
                self.scope['driver_accept'].assert_not_called()

    def test_deadline_rejection_precedes_acceptance(self):
        self.scope['_live_eta_offer'].return_value = None
        self.accept(None, self.user, 7, 5)
        self.scope['driver_accept'].assert_not_called()

    def test_custom_button_only_sets_input_state_no_accept_or_timer_reset(self):
        self.custom(None, self.user, 7)
        self.scope['driver_accept'].assert_not_called()
        self.scope['set_state'].assert_called_once_with(None, 101, 'offer_eta', {'order_id': 7}, merge=False)

    def test_custom_input_is_processed_before_offer_lock(self):
        tree = ast.parse(source('bot/handlers.py'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'handle_driver')
        text = ast.get_source_segment(source('bot/handlers.py'), node)
        self.assertLess(text.index('if state == States.D_OFFER_ETA and not cmd:'), text.index('if pending_offer and cmd not in'))

    def test_stale_order_foreign_driver_and_expired_offer_are_rejected(self):
        session = Mock()
        order = NS(id=7, status='searching', offered_driver_id=1, driver_id=None, parallel_driver_id=None, passenger_id=2)
        query = session.query.return_value.filter.return_value.with_for_update.return_value
        query.one_or_none.return_value = order
        scope = dict(Order=NS(id=0), offer_deadline_passed=Mock(return_value=False),
                     blacklist_service=NS(blocks=Mock(return_value=False)))
        live = load('bot/handlers.py', '_live_eta_offer', scope)
        self.assertIs(live(session, self.user, 7), order)
        for attr, value in [('status','assigned'), ('offered_driver_id',2), ('driver_id',2), ('parallel_driver_id',2)]:
            original = getattr(order, attr);setattr(order,attr,value)
            self.assertIsNone(live(session,self.user,7));setattr(order,attr,original)
        scope['offer_deadline_passed'].return_value=True
        self.assertIsNone(live(session,self.user,7))
        scope['offer_deadline_passed'].return_value=False
        scope['blacklist_service'].blocks.return_value=True
        self.assertIsNone(live(session,self.user,7))


class BlacklistTests(unittest.TestCase):
    def setUp(self):
        self.user = NS(id=1, vk_id=101)
        self.scope = dict(vk=Mock(), re=re, kb=NS(blacklist_menu=Mock(return_value='menu')),
                          blacklist_service=NS(add=Mock()), show_blacklist=Mock())
        self.callback = load('bot/handlers.py', 'add_blacklist_link', self.scope)

    def test_vk_ru_link_is_resolved_and_added_privately(self):
        self.scope['vk'].resolve_user_id.return_value = 201
        self.callback(None, self.user, 'https://vk.ru/client_name')
        self.scope['blacklist_service'].add.assert_called_once_with(None, self.user, 201)
        self.assertTrue(all(c.args[0] == 101 for c in self.scope['vk'].send_message.call_args_list))

    def test_vk_com_mobile_and_numeric_links_are_supported(self):
        for link in ('https://vk.com/id201', 'vk.ru/id201', 'https://m.vk.ru/client_name', 'https://www.vk.ru/id201/'):
            with self.subTest(link=link):
                self.scope['vk'].resolve_user_id.return_value=201
                self.scope['blacklist_service'].add.reset_mock()
                self.callback(None,self.user,link)
                self.scope['blacklist_service'].add.assert_called_once()

    def test_foreign_hosts_groups_self_and_failed_resolve_are_not_added(self):
        for link, vk_id in [('https://evil.test/id201',201),('https://vk.ru.evil.test/id201',201),('https://vk.ru/club123',None),
                            ('https://vk.ru/id101',101),('https://vk.ru/deleted',0),('https://vk.ru/club123',-123)]:
            with self.subTest(link=link,vk_id=vk_id):
                self.scope['vk'].resolve_user_id.return_value=vk_id
                self.scope['blacklist_service'].add.reset_mock()
                self.callback(None,self.user,link)
                self.scope['blacklist_service'].add.assert_not_called()

    def test_removal_is_scoped_to_owner(self):
        session=Mock();scope=dict(DriverBlacklist=object,vk=Mock(),kb=Mock(),show_blacklist_remove=Mock())
        callback=load('bot/handlers.py','remove_blacklist',scope)
        callback(session,self.user,201)
        session.query.return_value.filter_by.assert_called_once_with(driver_id=1,passenger_vk_id=201)
        session.query.return_value.filter_by.return_value.delete.assert_called_once()

    def test_add_is_idempotent_and_flushes_new_row(self):
        session=Mock();existing=NS(id=9)
        session.query.return_value.filter_by.return_value.one_or_none.return_value=existing
        scope=dict(User=NS(id=0),DriverBlacklist=Mock())
        callback=load('bot/blacklist_service.py','add',scope)
        self.assertIs(callback(session,self.user,201),existing)
        session.add.assert_not_called()
        session.query.return_value.filter_by.return_value.one_or_none.return_value=None
        callback(session,self.user,201)
        session.add.assert_called_once();session.flush.assert_called_once()

    def test_rating_adds_without_review_and_keeps_driver_state(self):
        session=Mock();passenger=NS(vk_id=201)
        order=NS(id=7,driver_id=1,passenger_id=2,status='completed',dispatcher_id=None,driver_rating_prompt_outbox_id=8)
        session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
        session.get.return_value=passenger
        scope=dict(Order=NS(id=0),User=object,blacklist_service=NS(add=Mock()),_finish_rating_prompt=Mock(),vk=Mock())
        load('bot/handlers.py','blacklist_from_rating',scope)(session,self.user,7)
        scope['blacklist_service'].add.assert_called_once_with(session,self.user,201)
        scope['_finish_rating_prompt'].assert_called_once()
        session.add.assert_not_called()
        self.assertTrue(all(c.args[0]==101 for c in scope['vk'].send_message.call_args_list))

    def test_expired_or_foreign_rating_button_does_nothing(self):
        session=Mock();scope=dict(Order=NS(id=0),User=object,blacklist_service=NS(add=Mock()),_finish_rating_prompt=Mock(),vk=Mock())
        callback=load('bot/handlers.py','blacklist_from_rating',scope)
        for order in (None,NS(driver_id=2),NS(driver_id=1,status='assigned'),
                      NS(driver_id=1,status='completed',driver_rating_prompt_outbox_id=None)):
            session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
            callback(session,self.user,7)
        scope['blacklist_service'].add.assert_not_called()

    def test_remove_list_is_paginated_below_vk_ten_button_limit(self):
        scope=dict(close_blacklist_remove=Mock(),_blacklist_clients=lambda *a:[(i,f'Client {i}') for i in range(30)],
                   show_blacklist=Mock(),vk=Mock(),kb=keyboards(),set_state=Mock(),States=NS(D_MENU='menu'))
        callback=load('bot/handlers.py','show_blacklist_remove',scope)
        callback(None,self.user,2)
        raw=scope['vk'].send_tracked_message.call_args.kwargs['keyboard']
        self.assertLessEqual(len(actions(raw)),10)
        self.assertEqual(len([p for _,p in actions(raw) if p['cmd']=='blacklist_remove']),7)


class DispatchRegressionTests(unittest.TestCase):
    def test_source_has_guards_in_all_claim_paths(self):
        for path,name in [('bot/handlers.py','driver_accept'),('bot/handlers.py','select_direct_driver'),
                          ('bot/handlers.py','driver_take_from_chat'),('bot/handlers.py','reserve_eta_offer'),
                          ('bot/parallel_orders.py','take'),('bot/parallel_orders.py','_auto_offer_trip_is_eligible'),
                          ('bot/booking_service.py','take_booking'),('bot/customer_price_service.py','reserve_from_chat'),
                          ('bot/admin_order_assign_service.py','assign_from_admin')]:
            with self.subTest(path=path,name=name):
                tree=ast.parse(source(path));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
                self.assertIn('blacklist_service.blocks',ast.get_source_segment(source(path),node))

    def test_fifo_excludes_blacklisted_front_without_reordering_queue(self):
        text=source('bot/order_service.py');tree=ast.parse(text)
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='offer_to_next_driver')
        function=ast.get_source_segment(text,node)
        self.assertIn('blacklist_service.excluded_driver_ids',function)
        self.assertIn('exclude_driver_ids=sorted(excluded)',function)
        self.assertNotIn('move_to_tail',function)

    def test_next_driver_timeout_keeps_same_queue_handoff(self):
        text=source('bot/order_service.py');tree=ast.parse(text)
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_accept_timeout')
        function=ast.get_source_segment(text,node)
        self.assertIn('driver.is_on_line = False',function)
        self.assertIn('driver.driver_status = "offline"',function)
        self.assertIn('offer_to_next_driver(session, order, {driver_id})',function)
        self.assertNotIn('publish_direct_to_requests_chat(session, order)',function)

    def test_ordinary_and_direct_offers_always_schedule_sixty_seconds(self):
        for name in ('offer_to_next_driver','offer_to_selected_driver'):
            tree=ast.parse(source('bot/order_service.py'))
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            assignments=[n for n in ast.walk(node) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='timeout' for t in n.targets)]
            self.assertEqual(len(assignments),1)
            self.assertEqual(ast.literal_eval(assignments[0].value),60)

    def test_pending_and_chat_taps_reserve_without_early_assignment(self):
        for name in ('driver_take_pending','driver_take_from_chat'):
            tree=ast.parse(source('bot/handlers.py'));node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            function=ast.get_source_segment(source('bot/handlers.py'),node)
            self.assertIn('reserve_eta_offer',function)
            self.assertNotIn('order.driver_id = user.id',function)

    def test_new_migration_is_single_head_and_preserves_existing_tables(self):
        import pathlib
        root=pathlib.Path(__file__).resolve().parents[1]
        revisions={}
        for p in (root/'migrations/versions').glob('*.py'):
            scope={}
            for node in ast.parse(p.read_text()).body:
                if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('revision','down_revision') for t in node.targets):
                    for t in node.targets:
                        if isinstance(t,ast.Name):scope[t.id]=ast.literal_eval(node.value)
            if scope.get('revision'):revisions[scope['revision']]=scope['down_revision']
        heads=set(revisions)-set(revisions.values())
        self.assertEqual(heads,{'0059_driver_blacklist'})
        text=source('migrations/versions/0059_driver_blacklist.py')
        upgrade=text.split('def upgrade():',1)[1].split('def downgrade():',1)[0]
        self.assertNotIn('drop_',upgrade)
        self.assertIn('has_table("driver_blacklist")',upgrade)


class AcceptanceRuntimeTests(unittest.TestCase):
    def run_accept(self, age=60, old_prompt=False):
        order=NS(id=7,number=7,status='searching',passenger_id=2,driver_id=None,
                 offered_driver_id=1,dispatcher_id=None,customer_price=None,chat_notice_outbox_id=None,
                 eta_offer_source='fifo',created_at=NOW-dt.timedelta(seconds=age),
                 driver_accept_time=None,arrival_eta=None)
        driver=NS(id=1,vk_id=101,is_on_line=True,current_line='Горнозаводск',driver_missed_offers=0)
        session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
        scope=dict(Order=NS(id=0),order_service=Mock(),parallel_orders=NS(local_line_allows=lambda *a:True),
                   blacklist_service=NS(blocks=lambda *a:False),offer_deadline_passed=lambda *a:False,
                   driver_block_service=NS(is_blocked=lambda u:False),lines=NS(has_complete_car=lambda u:True),
                   timers=Mock(),time_utils=NS(now=lambda:NOW),queue_service=Mock(),audit=Mock(),
                   request_claim_actuality=Mock(return_value=old_prompt),delivery_service=NS(is_delivery=lambda o:False),
                   _apply_eta=Mock(),vk=Mock())
        scope['order_service']._current_offer.return_value=1
        scope['claim_needs_actuality']=load('bot/handlers.py','claim_needs_actuality',dict(dt=dt,time_utils=scope['time_utils']))
        load('bot/handlers.py','driver_accept',scope)(session,driver,7,notify_passenger=False,selected_eta=10)
        return order,scope

    def test_eta_is_applied_in_the_same_acceptance_transaction(self):
        order,scope=self.run_accept()
        self.assertEqual(order.driver_id,1)
        self.assertEqual(order.status,'assigned')
        self.assertIsNone(order.offered_driver_id)
        self.assertEqual(order.driver_accept_time,NOW)
        self.assertEqual(order.arrival_eta,10)
        scope['_apply_eta'].assert_called_once()
        scope['timers'].cancel.assert_any_call('accept',7)

    def test_old_request_keeps_chosen_eta_while_waiting_for_passenger(self):
        order,scope=self.run_accept(age=121,old_prompt=True)
        self.assertEqual(order.arrival_eta,10)
        scope['_apply_eta'].assert_not_called()
        scope['request_claim_actuality'].assert_called_once()
        scope['order_service'].finalize_offer_message.assert_called_once_with(
            unittest.mock.ANY,order,'⏰ Вы выбрали 10 мин.')

    def test_acceptance_does_not_send_extra_found_driver_notice(self):
        _,scope=self.run_accept()
        scope['order_service'].notify_driver_found.assert_not_called()


if __name__ == '__main__':
    unittest.main()
