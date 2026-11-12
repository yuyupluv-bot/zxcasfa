"""Offline regression checks: execute original functions with mocked I/O.

No installed dependencies or live VK/database access are needed.
"""
import datetime as dt
import re
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from test_requirements_v173_requested_changes import load, relative_modules, source


class ArrivalCleanupV187(unittest.TestCase):
    def arrival(self):
        order = NS(id=7, number=7, passenger_id=2, driver_id=1,
                   dispatcher_id=None, departure_prompt_outbox_id=None,
                   enroute_notice_outbox_id=None, arrival_notice_outbox_id=None)
        passenger = NS(vk_id=201)
        driver = NS(vk_id=101, car_model='Гранта', car_color='Серая', car_number='817')
        session = Mock()
        session.get.side_effect = lambda model, key: passenger if key == 2 else driver
        vk = Mock()
        vk.send_tracked_message.return_value = 88
        scope = dict(dt=dt, re=re, timers=Mock(), waiting_service=Mock(),
                     get_int=lambda *a: 3, get_float=lambda *a: 10,
                     User=object, vk=vk, kb=Mock(), set_state=Mock(),
                     States=NS(P_ARRIVED='arrived'), _is_dispatcher_order=lambda o: False)
        load('bot/order_service.py', 'start_free_waiting', scope)(session, order)
        return order, session, vk, driver, passenger

    def seat(self, order, session, vk, driver, passenger, delete_result=True):
        outbox = types.ModuleType('v173_runtime.outbox_service')
        outbox.cancel_or_delete = Mock(return_value=delete_result)
        session.get.return_value = passenger
        session.get.side_effect = None
        scope = dict(__package__='v173_runtime', active_order_for=Mock(return_value=order),
                     show_main_menu=Mock(), waiting_service=Mock(), timers=Mock(),
                     _is_dispatcher_order=lambda o: bool(o.dispatcher_id), User=object,
                     vk=vk, kb=Mock(), set_state=Mock(), States=NS(P_IN_RIDE='ride', D_IN_RIDE='ride'),
                     delivery_service=NS(is_partner_delivery=lambda o: False),
                     order_service=NS(multi_car_label=Mock(return_value='')),
                     _driver_ride_kb=Mock(return_value='driver-keyboard'))
        scope['waiting_service'].snapshot.return_value = (0, 0)
        with relative_modules(outbox_service=outbox):
            load('bot/handlers.py', 'driver_seated', scope)(session, driver)
        return outbox, scope

    def test_arrival_card_keeps_exact_text_and_tracks_its_id(self):
        order, session, vk, driver, passenger = self.arrival()
        self.assertEqual(order.arrival_notice_outbox_id, 88)
        self.assertEqual(vk.send_tracked_message.call_args.args,
                         (201, '🚘 Водитель на месте! Машина: Гранта, Серая, 817\n'
                          '⏰ У вас 3 мин бесплатного ожидания, далее 10 руб./мин.'))
        self.assertIsNotNone(vk.send_tracked_message.call_args.kwargs['keyboard'])
        vk.send_message.assert_not_called()

    def test_boarding_removes_only_arrival_card_and_keeps_trip_message(self):
        order, session, vk, driver, passenger = self.arrival()
        order.departure_prompt_outbox_id = 44
        order.enroute_notice_outbox_id = 45
        outbox, scope = self.seat(order, session, vk, driver, passenger)
        outbox.cancel_or_delete.assert_called_once_with(session, 88)
        self.assertIsNone(order.arrival_notice_outbox_id)
        self.assertEqual(order.departure_prompt_outbox_id, 44)
        self.assertEqual(order.enroute_notice_outbox_id, 45)
        self.assertEqual(order.status, 'in_progress')
        self.assertIn((201, '🚗 Поехали! Хорошей поездки.'),
                      [c.args for c in vk.send_message.call_args_list])
        scope['timers'].cancel.assert_called_once_with('waiting', 7)
        scope['waiting_service'].stop_waiting.assert_called_once_with(session, order)

    def test_repeated_boarding_does_not_delete_another_message(self):
        order, session, vk, driver, passenger = self.arrival()
        self.seat(order, session, vk, driver, passenger)
        outbox, _ = self.seat(order, session, vk, driver, passenger)
        outbox.cancel_or_delete.assert_not_called()

    def test_old_order_without_tracking_can_still_start(self):
        order, session, vk, driver, passenger = self.arrival()
        del order.arrival_notice_outbox_id
        outbox, _ = self.seat(order, session, vk, driver, passenger)
        outbox.cancel_or_delete.assert_not_called()
        self.assertEqual(order.status, 'in_progress')

    def test_cleanup_failure_keeps_reference_without_blocking_trip(self):
        order, session, vk, driver, passenger = self.arrival()
        self.seat(order, session, vk, driver, passenger, delete_result=False)
        self.assertEqual(order.arrival_notice_outbox_id, 88)
        self.assertEqual(order.status, 'in_progress')
        self.assertIn((201, '🚗 Поехали! Хорошей поездки.'),
                      [c.args for c in vk.send_message.call_args_list])

    def test_dispatcher_flow_does_not_get_passenger_trip_text(self):
        order, session, vk, driver, passenger = self.arrival()
        order.dispatcher_id = 3
        order.arrival_notice_outbox_id = None
        vk.reset_mock()
        outbox, _ = self.seat(order, session, vk, driver, passenger)
        outbox.cancel_or_delete.assert_not_called()
        self.assertTrue(any('Пассажиры сели и поехали' in c.args[1]
                            for c in vk.send_message.call_args_list))
        self.assertFalse(any('Хорошей поездки' in c.args[1]
                             for c in vk.send_message.call_args_list))

    def test_durable_outbox_handles_unsent_inflight_and_sent_card(self):
        scope = dict(re=re, OutboxMessage=NS(id=Mock()), log=Mock(), dt=dt,
                     time_utils=NS(now=lambda: dt.datetime.now(dt.timezone.utc)))
        for name in ('_stored_vk_marker_id', '_stored_vk_message_id', '_stored_vk_cmid', 'cancel_or_delete'):
            load('bot/outbox_service.py', name, scope)
        for before, marker, after in (
            ('pending', None, 'cancelled'),
            ('failed', None, 'cancelled'),
            ('sending', None, 'cancel_requested'),
            ('sent', 'vk_message_id:1234', 'delete_requested'),
            ('sent', 'vk_cmid:55', 'delete_requested'),
        ):
            with self.subTest(status=before, marker=marker):
                row = NS(status=before, last_error=marker, claimed_at=None)
                session = Mock()
                session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = row
                self.assertTrue(scope['cancel_or_delete'](session, 88))
                self.assertEqual(row.status, after)

    def test_schema_contains_nullable_pointer_and_guard(self):
        self.assertIn('arrival_notice_outbox_id = Column(Integer, nullable=True)', source('common/models.py'))
        self.assertIn('ALTER TABLE orders ADD COLUMN IF NOT EXISTS arrival_notice_outbox_id INTEGER', source('common/db_migrate.py'))
        text = source('migrations/versions/0058_arrival_notice.py')
        self.assertIn('down_revision = "0057_parallel_accept_time"', text)
        self.assertIn('if "arrival_notice_outbox_id" not in columns:', text)


if __name__ == '__main__':
    unittest.main()
