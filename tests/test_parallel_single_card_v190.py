"""Parallel acceptance: real ORM and handlers, mocked VK/timers; no network."""
import datetime as dt
import json
from unittest.mock import patch
import unittest
import integration_v181_fixes as fixture
from common import models as m, time_utils

class ParallelSingleCardTests(unittest.TestCase):
    scope = fixture.ImportedFlowTests.scope
    tracked = fixture.ImportedFlowTests.tracked
    order = fixture.ImportedFlowTests.order
    current_and_parallel = fixture.ImportedFlowTests.current_and_parallel
    setUpClass = fixture.ImportedFlowTests.__dict__['setUpClass']
    tearDownClass = fixture.ImportedFlowTests.__dict__['tearDownClass']
    setUp = fixture.ImportedFlowTests.setUp
    tearDown = fixture.ImportedFlowTests.tearDown
    def accept(self, *, old=False, dispatcher=False, source="manual"):
        current = self.order(passenger_id=self.previous.id, status='in_progress', public_number=2999)
        now = time_utils.utc_now()
        order = self.order(driver_id=None, status='queued',
                           created_at=now - dt.timedelta(minutes=3 if old else 0),
                           dispatcher_id=self.dispatcher.id if dispatcher else None,
                           public_number=3001)
        self.driver.driver_status = 'busy'
        self.s.flush()
        with patch.object(self.parallel, '_free_driver_has_priority', return_value=False), \
             patch.object(self.parallel, '_destination_restricted_orders', side_effect=lambda c, rows: rows), \
             patch.object(self.parallel, 'refresh_busy_driver_menus'):
            self.parallel.take(self.s, self.driver, order.id, from_chat=(source == "chat"))
            self.assertIsNone(order.parallel_driver_id)
            if source == "auto":
                order.eta_offer_source = "parallel_auto"
            self.vk.reset_mock()
            self.parallel.save_eta(self.s, self.driver, order.id, 7)
        return current, order

    def passenger_calls(self):
        calls = self.vk.send_message.call_args_list + self.vk.send_tracked_message.call_args_list
        return [c for c in calls if c.args and c.args[0] == self.passenger.vk_id]

    def card(self, order, old=False):
        pid = order.chat_actuality_prompt_outbox_id if old else order.departure_prompt_outbox_id
        row = self.s.get(m.OutboxMessage, pid)
        self.assertIsNotNone(row)
        self.assertEqual(row.peer_id, self.passenger.vk_id)
        self.assertEqual(row.text, self.orders.departure_driver_card_text(self.driver) + '\n'
                         'Водитель завершает текущую поездку и будет у вас ориентировочно через 7 мин.')
        keyboard=json.loads(row.keyboard)
        self.assertTrue(keyboard['inline'])
        actions=[b['action'] for r in keyboard['buttons'] for b in r]
        self.assertEqual([a['label'] for a in actions], ['✅ Да, жду','❌ Нет, отменяю заявку'])
        commands=[json.loads(a['payload']) for a in actions]
        self.assertTrue(all(c['order_id']==order.id for c in commands))
        return row,commands

    def test_fresh_one_card_yes_is_idempotent(self):
        current, order=self.accept()
        self.assertEqual(len(self.passenger_calls()),1)
        row, actions=self.card(order)
        self.assertEqual([a['cmd'] for a in actions], ['departure_wait','departure_cancel'])
        self.vk.reset_mock()
        self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        self.assertEqual(len(self.passenger_calls()),0)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertEqual(sum(c.args==(101,'Клиент по параллельной заявке, ожидает на месте') for c in self.vk.send_message.call_args_list),1)
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(order.status,'parallel_assigned')
        self.vk.reset_mock()
        self.parallel.save_eta(self.s,self.driver,order.id,7)
        self.assertEqual(len(self.passenger_calls()),0)

    def test_old_one_card_yes_keeps_eta_and_no_second_card(self):
        current,order=self.accept(old=True)
        self.assertEqual(len(self.passenger_calls()),1)
        row,actions=self.card(order,True)
        self.assertEqual([a['cmd'] for a in actions], ['chat_order_actual_yes','chat_order_actual_no'])
        self.vk.reset_mock()
        with patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,True)
            self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,True)
        self.assertEqual(len(self.passenger_calls()),0)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertEqual(order.parallel_eta,7)
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(sum(c.args==(101,'Клиент по параллельной заявке, ожидает на месте') for c in self.vk.send_message.call_args_list),1)

    def check_cancel(self, old):
        current,order=self.accept(old=old)
        row,_=self.card(order,old)
        with patch.object(self.handlers.passenger_queue,'try_promote'), patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            if old: self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,False)
            else: self.handlers.passenger_departure_response(self.s,self.passenger,True,order.id)
        self.assertEqual(order.status,'cancelled')
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertEqual(self.s.query(m.FakeCall).filter_by(order_id=order.id).count(),0)

    def test_fresh_no_cancels_only_next_trip(self):
        self.check_cancel(False)

    def test_old_no_cancels_only_next_trip(self):
        self.check_cancel(True)

    def test_chat_and_auto_offer_use_one_card(self):
        current,order=self.accept(source='chat')
        self.assertEqual(len(self.passenger_calls()),1)
        self.card(order)
        self.assertEqual(current.status,'in_progress')

    def test_automatic_offer_uses_one_card(self):
        current,order=self.accept(source='auto')
        self.assertEqual(len(self.passenger_calls()),1)
        self.card(order)
        self.assertEqual(current.status,'in_progress')

    def test_legacy_old_prompt_is_upgraded_in_place_on_yes(self):
        current,order=self.accept(old=True)
        row,_=self.card(order,True)
        row.text='У нас есть водитель. Вы ждёте машинку?'
        self.vk.reset_mock()
        with patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,True)
        self.assertIn('Москвич',row.text)
        self.assertIn('через 7 мин.',row.text)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertEqual(len(self.passenger_calls()),0)
        self.assertEqual(current.status,'in_progress')

    def test_late_cancel_still_creates_only_one_false_call(self):
        from bot import fake_calls_service
        current,order=self.accept()
        order.parallel_accepted_at=time_utils.utc_now()-dt.timedelta(seconds=121)
        self.s.flush()
        with patch.object(fake_calls_service,'_schedule_reminder'), \
             patch.object(self.handlers.passenger_queue,'try_promote'), \
             patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.passenger_departure_response(self.s,self.passenger,True,order.id)
            self.handlers.passenger_departure_response(self.s,self.passenger,True,order.id)
        self.assertEqual(order.status,'cancelled')
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(self.s.query(m.FakeCall).filter_by(order_id=order.id).count(),1)

    def test_old_timeout_still_cancels_without_current_trip(self):
        from contextlib import contextmanager
        current,order=self.accept(old=True)
        prompt=order.chat_actuality_prompt_outbox_id
        @contextmanager
        def session_scope():
            yield self.s
        with patch('common.database.session_scope',session_scope), \
             patch.object(self.handlers.passenger_queue,'try_promote'), \
             patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.chat_actuality_timeout(order.id)
        self.assertEqual(order.status,'cancelled')
        self.assertEqual(current.status,'in_progress')
        self.assertIsNone(order.chat_actuality_prompt_outbox_id)
        self.assertIsNotNone(prompt)
        self.assertEqual(self.s.query(m.FakeCall).filter_by(order_id=order.id).count(),0)

    def test_stale_eta_before_confirmation_does_not_send_again(self):
        current,order=self.accept(old=True)
        row,_=self.card(order,True)
        self.vk.reset_mock()
        self.parallel.save_eta(self.s,self.driver,order.id,7)
        self.assertEqual(len(self.passenger_calls()),0)
        self.assertEqual(order.chat_actuality_prompt_outbox_id,row.id)
        self.assertEqual(current.status,'in_progress')

    def test_other_passenger_cannot_answer(self):
        current,order=self.accept()
        row,_=self.card(order)
        self.vk.reset_mock()
        with patch.object(self.handlers,'show_main_menu'):
            self.handlers.passenger_departure_response(self.s,self.other,True,order.id)
        self.assertEqual(order.status,'parallel_assigned')
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(len(json.loads(row.keyboard)['buttons']),2)
        self.assertFalse(any(c.args and c.args[0]==self.driver.vk_id for c in self.vk.send_message.call_args_list))

    def test_promotion_preserves_prompt_until_answer(self):
        current,order=self.accept()
        row,_=self.card(order)
        current.status='completed'
        self.s.flush()
        with patch.object(self.handlers,'_driver_ride_kb',return_value='ride'), \
             patch.object(self.orders,'schedule_prearrival_notice'):
            self.parallel.promote_after_current(self.s,self.driver)
            self.vk.reset_mock()
            self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        self.assertEqual(order.status,'assigned')
        self.assertEqual(order.driver_id,self.driver.id)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertEqual(len(self.passenger_calls()),0)

    def test_dispatcher_receives_existing_notice_no_prompt(self):
        current,order=self.accept(dispatcher=True)
        self.assertIsNone(order.departure_prompt_outbox_id)
        self.assertIsNone(order.chat_actuality_prompt_outbox_id)
        self.assertEqual(len(self.passenger_calls()),1)
        self.assertIn('Для заявки',self.passenger_calls()[0].args[1])
        self.assertEqual(current.status,'in_progress')
