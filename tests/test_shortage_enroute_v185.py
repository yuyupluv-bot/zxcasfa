"""Offline checks for opt-in notices and passenger arrival."""
import datetime as dt
import json
from unittest.mock import Mock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from common import models as m, time_utils


@patch('vk_api.bot_longpoll.VkBotLongPoll')
def test_shortage_opt_in_and_one_minute(_longpoll):
    from bot import shortage_notice_service as service
    engine = create_engine('sqlite:///:memory:')
    m.Base.metadata.create_all(engine)
    s = sessionmaker(engine, autoflush=False, expire_on_commit=False)()
    d = m.User(vk_id=1, role='driver', granted_roles='driver,passenger',
               is_on_line=True, driver_status='away', shortage_notice_enabled=True)
    other = m.User(vk_id=2, role='driver', granted_roles='driver,passenger',
                   is_on_line=False, driver_status='offline', shortage_notice_enabled=False)
    p = m.User(vk_id=3, role='passenger', granted_roles='passenger')
    city = m.City(name='Горнозаводск',is_active=True)
    s.add_all([d,other,p,city]);s.flush()
    q = m.DriverQueue(driver_id=d.id, city_id=city.id, status='away', position=1)
    order = m.Order(passenger_id=p.id,address_from='a',address_to='b',status='queued',
                    created_at=time_utils.now()-dt.timedelta(seconds=59))
    s.add_all([q,order]);s.flush()
    def tracked(peer,text):
        row=m.OutboxMessage(peer_id=peer,text=text,status='pending',random_id=10)
        s.add(row);s.flush();return row.id
    with patch.object(service.vk,'send_tracked_message',side_effect=tracked) as send, patch.object(service.outbox_service,'cancel_or_delete',return_value=True) as delete:
        service.reconcile(s); assert send.call_count == 0
        order.created_at=time_utils.now()-dt.timedelta(seconds=65);s.flush()
        service.reconcile(s); assert send.call_count==1
        assert send.call_args.args==(1,service.NOTICE)
        notice=d.shortage_notice_outbox_id;assert notice
        service.reconcile(s); assert send.call_count==1
        assert not other.shortage_notice_outbox_id
        # A free driver alone is insufficient to clear the notice.
        d.driver_status='online';q.status='waiting';s.flush()
        service.reconcile(s);delete.assert_not_called();assert d.shortage_notice_outbox_id == notice
        # Likewise, no pending orders alone is insufficient.
        d.driver_status='away';q.status='away';order.status='cancelled';s.flush()
        service.reconcile(s);delete.assert_not_called();assert d.shortage_notice_outbox_id == notice
        # Both conditions together clear exactly the tracked message.
        d.driver_status='online';q.status='waiting';s.flush()
        service.reconcile(s);delete.assert_called_once_with(s,notice)
        assert d.shortage_notice_outbox_id is None
    s.close();engine.dispose()


@patch('vk_api.bot_longpoll.VkBotLongPoll')
def test_arrival_deletes_exact_enroute_and_displays_car(_longpoll):
    from bot import order_service
    engine=create_engine('sqlite:///:memory:');m.Base.metadata.create_all(engine)
    s=sessionmaker(engine,autoflush=False,expire_on_commit=False)()
    d=m.User(vk_id=10,car_model='Лада',car_color='белая',car_number='А123АА')
    p=m.User(vk_id=20);s.add_all([d,p]);s.flush()
    notice=m.OutboxMessage(peer_id=20,text='Машинка в пути',status='pending',random_id=11)
    s.add(notice);s.flush()
    order=m.Order(passenger_id=p.id,driver_id=d.id,address_from='a',address_to='b',status='assigned',enroute_notice_outbox_id=notice.id)
    s.add(order);s.flush()
    with patch.object(order_service.vk,'send_message') as send, patch.object(order_service.waiting_service,'start_waiting'), patch.object(order_service.timers,'cancel'):
        order_service.start_free_waiting(s,order)
    assert notice.status=='cancelled'
    assert order.enroute_notice_outbox_id is None
    assert send.call_args.args[1]=='🚘 Водитель на месте! Машина: Лада, белая, А123АА\n⏰ У вас 3 мин бесплатного ожидания, далее 10 руб./мин.'
    s.close();engine.dispose()


def test_toggle_is_directly_below_payment_toggle_and_defaults_off():
    from bot.keyboards import driver_settings_keyboard
    kb=json.loads(driver_settings_keyboard(False))
    rows=kb['buttons']
    actions=[(item['action']['label'],json.loads(item['action']['payload'])['cmd']) for row in rows for item in row]
    idx=next(i for i,(_,cmd) in enumerate(actions) if cmd=='payment_toggle')
    assert actions[idx+1]==('❌ Уведомлять, при нехватке водителей','shortage_notice_toggle')
    kb=json.loads(driver_settings_keyboard(False,None,True))
    assert kb['buttons'][4][0]['action']['label'].startswith('✅')


@patch('vk_api.bot_longpoll.VkBotLongPoll')
def test_offer_counts_as_not_taken_but_is_not_free_for_alert(_longpoll):
    from bot import shortage_notice_service as service
    engine=create_engine('sqlite:///:memory:');m.Base.metadata.create_all(engine)
    s=sessionmaker(engine,autoflush=False)()
    d=m.User(vk_id=10,role='driver',granted_roles='passenger,driver',is_on_line=True,driver_status='busy',shortage_notice_enabled=True)
    p=m.User(vk_id=20);s.add_all([d,p]);s.flush()
    order=m.Order(passenger_id=p.id,address_from='a',address_to='b',status='searching',offered_driver_id=d.id,created_at=time_utils.now()-dt.timedelta(minutes=3))
    s.add(order);s.flush()
    with patch.object(service.vk,'send_tracked_message') as send:
        service.reconcile(s);send.assert_not_called()
    assert service.waiting_query(s).first() is not None
    s.close();engine.dispose()
