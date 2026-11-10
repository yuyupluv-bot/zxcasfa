"""Integration checks with real ORM and no VK or Firebase network access."""
import datetime as dt
import json
import unittest
from unittest.mock import Mock, patch
from integration_v181_fixes import ImportedFlowTests
from common import models as m, time_utils
from bot import keyboards as kb


class ParallelChangesV186(ImportedFlowTests):
    def _cancel_test(self, elapsed, promoted=False):
        from bot import fake_calls_service, passenger_queue
        base = time_utils.utc_now()
        if promoted:
            order=self.order(driver_id=self.driver.id, status='assigned', parallel_accepted_at=base-dt.timedelta(seconds=elapsed),
                             driver_departed_at=base, driver_accept_time=base)
        else:
            current,order=self.current_and_parallel()
            order.parallel_accepted_at=base-dt.timedelta(seconds=elapsed)
        self.s.flush()
        with patch.object(time_utils,'now',return_value=base), patch.object(fake_calls_service,'_schedule_reminder'), \
             patch.object(passenger_queue,'try_promote'), patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.passenger_cancel(self.s,self.passenger,order.id)
        self.s.flush()
        return order, self.s.query(m.FakeCall).filter(m.FakeCall.order_id==order.id).count()

    def test_cancel_120_seconds_during_current_ride_is_free(self):
        order,count=self._cancel_test(120)
        self.assertEqual(order.status,'cancelled');self.assertEqual(count,0)
        self.assertFalse(self.passenger.passenger_fake_call_blocked)
        current=self.s.query(m.Order).filter(m.Order.driver_id==self.driver.id,m.Order.status=='in_progress').one()
        self.assertEqual(current.passenger_id,self.previous.id)

    def test_cancel_over_120_seconds_during_current_ride_creates_one_false_call(self):
        order,count=self._cancel_test(121)
        self.assertEqual(count,1);self.assertTrue(self.passenger.passenger_fake_call_blocked)
        self.assertEqual(self.s.query(m.Order).filter(m.Order.driver_id==self.driver.id,m.Order.status=='in_progress').count(),1)
        self.handlers.passenger_cancel(self.s,self.passenger,order.id)
        self.assertEqual(self.s.query(m.FakeCall).filter(m.FakeCall.order_id==order.id).count(),1)

    def test_cancel_after_promotion_charges_even_within_two_minutes(self):
        order,count=self._cancel_test(1,promoted=True)
        self.assertEqual(order.status,'cancelled');self.assertEqual(count,1)

    def test_old_unknown_timestamp_does_not_invent_debt(self):
        _,order=self.current_and_parallel();order.parallel_accepted_at=None;self.s.flush()
        with patch.object(self.handlers.passenger_queue,'try_promote'), patch.object(self.handlers,'_driver_ride_kb',return_value='ride'):
            self.handlers.passenger_cancel(self.s,self.passenger,order.id)
        self.s.flush();self.assertEqual(self.s.query(m.FakeCall).count(),0)

    def test_parallel_yes_notifies_the_correct_driver_once(self):
        _,order=self.current_and_parallel();order.parallel_accepted_at=time_utils.now()
        order.departure_prompt_outbox_id=self.tracked(201,'Водитель завершает поездку\nВы ждёте машинку?',keyboard=kb.passenger_departure_keyboard(order.id))
        self.s.flush()
        self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        matches=[c for c in self.vk.send_message.call_args_list if c.args==(101,'Клиент по параллельной заявке, ожидает на месте')]
        self.assertEqual(len(matches),1)
        self.assertEqual(self.s.get(m.OutboxMessage,order.enroute_notice_outbox_id).text,'Машинка в пути')
        self.assertIsNone(order.departure_prompt_outbox_id)

    def test_no_false_call_reason_never_creates_a_penalty(self):
        from bot import fake_calls_service, passenger_queue
        for status in ('assigned','arrived','in_progress'):
            with self.subTest(status=status):
                self.s.query(m.Order).delete();self.s.flush()
                order=self.order(status=status,driver_departed_at=time_utils.now()-dt.timedelta(hours=1),
                                 parallel_accepted_at=time_utils.now()-dt.timedelta(hours=2))
                with patch.object(fake_calls_service,'create') as create, patch.object(passenger_queue,'try_promote'), \
                     patch.object(self.handlers,'_leave_temporary_chat_line'):
                    self.handlers.driver_cancel_active(self.s,self.driver,'no_false_call')
                create.assert_not_called();self.assertEqual(order.status,'cancelled')
                self.assertTrue(any(c.args==(201,'Водитель отменил заявку, ложного вызова не будет.') for c in self.vk.send_message.call_args_list))
                self.vk.reset_mock()

    def test_taking_new_parallel_sends_question_to_correct_passenger(self):
        current=self.order(passenger_id=self.previous.id,status='in_progress',public_number=2999)
        order=self.order(driver_id=None,status='queued',created_at=time_utils.utc_now())
        self.driver.driver_status='busy';self.s.flush()
        with patch.object(self.parallel,'_free_driver_has_priority',return_value=False), \
             patch.object(self.parallel,'_destination_restricted_orders',side_effect=lambda current, rows: rows), \
             patch.object(self.parallel,'_remove_notifications'), \
             patch.object(self.parallel,'refresh_busy_driver_menus'):
            result=self.parallel.take(self.s,self.driver,order.id)
        self.assertEqual(result.id,order.id)
        self.assertIsNotNone(order.parallel_accepted_at)
        prompt=self.s.get(m.OutboxMessage,order.departure_prompt_outbox_id)
        self.assertEqual(prompt.peer_id,self.passenger.vk_id)
        self.assertIn('Вы ждёте машинку?',prompt.text)
        labels=[b['action']['label'] for row in json.loads(prompt.keyboard)['buttons'] for b in row]
        self.assertEqual(labels,['✅ Да, жду','❌ Нет, отменяю заявку'])
        self.assertEqual(current.status,'in_progress')
        self.assertEqual(order.status,'parallel_assigned')

    def test_old_parallel_confirmation_yes_notifies_only_once(self):
        _,order=self.current_and_parallel();order.parallel_accepted_at=time_utils.utc_now()
        order.chat_actuality_prompt_outbox_id=self.tracked(201,'Вы ждёте машинку?',keyboard=kb.parallel_actuality_keyboard(order.id))
        self.s.flush()
        with patch.object(self.parallel,'prompt_eta_after_actuality') as ask_eta:
            self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,True)
            self.handlers.passenger_chat_order_actual(self.s,self.passenger,order.id,True)
        ask_eta.assert_called_once()
        matches=[c for c in self.vk.send_message.call_args_list if c.args==(101,'Клиент по параллельной заявке, ожидает на месте')]
        self.assertEqual(len(matches),1)
        self.assertEqual(self.s.get(m.OutboxMessage,order.enroute_notice_outbox_id).text,'Машинка в пути')

    def test_driver_cancel_with_pending_parallel_promotes_without_penalty(self):
        from bot import fake_calls_service
        current, reserved = self.current_and_parallel()
        with patch.object(fake_calls_service,'create') as create, \
             patch.object(self.parallel,'promote_after_current') as promote, \
             patch.object(self.handlers,'_leave_temporary_chat_line'):
            self.handlers.driver_cancel_active(self.s,self.driver,'no_false_call')
        create.assert_not_called()
        promote.assert_called_once_with(self.s,self.driver)
        self.assertEqual(current.status,'cancelled')
        self.assertEqual(reserved.status,'parallel_assigned')

    def test_driver_cancel_keyboard_has_new_reason(self):
        keyboard=json.loads(kb.driver_active_cancel_keyboard())
        buttons=[b['action'] for row in keyboard['buttons'] for b in row]
        self.assertTrue(any(b['label']=='Без ложного вызова' and json.loads(b['payload'])['cmd']=='driver_cancel_no_false_call' for b in buttons))

    def test_warning_before_chargeable_cancel(self):
        base=time_utils.utc_now();_,order=self.current_and_parallel()
        order.parallel_accepted_at=base-dt.timedelta(seconds=121);self.s.flush()
        with patch.object(time_utils,'now',return_value=base):
            self.handlers.passenger_cancel_request(self.s,self.passenger,order.id)
        self.assertTrue(any('будет создан ложный вызов' in c.args[1] for c in self.vk.send_message.call_args_list if len(c.args)>1))
        self.assertEqual(order.status,'parallel_assigned')
