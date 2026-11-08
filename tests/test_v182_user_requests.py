"""Offline regression checks; VK network and production DB are never used."""
import datetime as dt
import json
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from common.models import Base, City, CommonChatNotice, DriverQueue, Order, OutboxMessage, User

with patch('vk_api.bot_longpoll.VkBotLongPoll.update_longpoll_server', return_value=None):
    from bot import parallel_orders as parallel, waiting_service as waiting, common_chat_notices as chat
    from bot import queue_service, passenger_queue, order_service
    from bot.main import _event_actor_id
    from vk_api.bot_longpoll import VkBotEventType

@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with sessionmaker(engine, autoflush=False, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


def make_order(text='', **kw):
    return Order(passenger_id=1, address_from=kw.pop('address_from', 'Адрес'),
                 address_to=kw.pop('address_to', 'Адрес'), route_text=text, **kw)

@pytest.mark.parametrize('field,text,expected', [
    ('route_text', 'заказ на вечер ПО ПАШИИ 200 рублей', 'Пашия'),
    ('address_from', 'ул. Ленина, по Кусье', 'Кусья'),
    ('address_to', 'нужно по Пашии', 'Пашия'),
    ('comment', 'по Кусье, двое', 'Кусья'),
    ('route_text', 'Пашия — Кусья', None),
    ('route_text', 'из Пашии в город', None),
    ('route_text', 'по Пашиина улице', None),
])
def test_local_phrases_any_order_field(field, text, expected):
    order = make_order('')
    setattr(order, field, text)
    assert parallel.local_village_line(order) == expected


def test_origin_route_not_confused_with_local_trip():
    order = make_order('Пашия — Кусья')
    assert parallel.local_village_line(order) is None
    assert parallel.route_priority_city(order) == 'Пашия'


def test_local_line_restriction():
    order = make_order('заявка по Кусье сейчас')
    kus = NS(is_on_line=True, current_line='Кусья')
    assert parallel.local_line_allows(order, kus)
    assert not parallel.local_line_allows(order, NS(is_on_line=True, current_line='Горнозаводск'))
    assert not parallel.local_line_allows(order, NS(is_on_line=False, current_line='Кусья'))


def test_unified_waiting_allowance_and_multiple_intervals():
    with patch.object(waiting, 'get_int', return_value=3), patch.object(waiting, 'get_float', return_value=10):
        assert waiting._cost_for(None, 120, 0) == (2, 0)
        assert waiting._cost_for(None, 420, 300) == (7, 40)
        assert waiting._cost_for(None, 600, 300) == (10, 70)
        order = NS(waiting_seconds=120, ride_waiting_seconds=0, waiting_started_at=None, status='in_progress')
        assert waiting.free_remaining_seconds(None, order) == 60
        now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        with patch.object(waiting, '_now', return_value=now):
            waiting.start_waiting(None, order)
        with patch.object(waiting, '_now', return_value=now + dt.timedelta(minutes=5)):
            assert waiting.snapshot(None, order) == (7, 40)
            assert waiting.free_remaining_seconds(None, order) == 0
            assert waiting.finalize(None, order) == (7, 40)
        assert order.waiting_seconds == 420
        assert order.waiting_cost == 40


def test_exact_dispatch_chooses_only_village_driver(db):
    a, b = City(name='Пашия', is_active=True), City(name='Горнозаводск', is_active=True)
    passenger=User(vk_id=1); one=User(vk_id=2, role='driver', granted_roles='driver', current_line='Пашия', is_on_line=True, driver_status='online', is_blocked=False)
    two=User(vk_id=3, role='driver', granted_roles='driver', current_line='Горнозаводск', is_on_line=True, driver_status='online', is_blocked=False)
    db.add_all([a,b,passenger,one,two]); db.flush()
    db.add_all([DriverQueue(driver_id=two.id,city_id=b.id,status='waiting',position=1), DriverQueue(driver_id=one.id,city_id=a.id,status='waiting',position=2)]); db.flush()
    with patch.object(queue_service, '_repair_visibly_free_drivers'):
        assert queue_service.next_waiting_driver(db, 'Пашия', line_scope='exact').id == one.id


def test_local_timeout_never_falls_back_to_gorno(db):
    passenger=User(vk_id=1);db.add(passenger);db.flush()
    order=make_order('заявка по Пашии', status='queued'); order.passenger_id=passenger.id
    db.add(order);db.flush()
    with patch.object(queue_service,'has_waiting_driver',return_value=False), patch.object(parallel,'refresh_busy_driver_menus'), patch.object(parallel.timers,'cancel'), patch.object(order_service,'offer_to_next_driver') as offer:
        parallel._fallback_to_free_drivers(db,order,recheck_village=True)
    assert order.status == 'queued'
    assert not order.parallel_route_fallback
    assert db.query(parallel.PassengerQueue).filter_by(order_id=order.id).count() == 1
    offer.assert_not_called()


@pytest.mark.parametrize('route,address_from,line', [
    ('из Пашии до Горнозаводска', 'Адрес', 'Пашия'),
    ('улица Ленина', 'Кусья, улица Ленина', 'Кусья'),
])
def test_parallel_notifications_for_departures(db, route, address_from, line):
    passenger=User(vk_id=1);driver=User(vk_id=2,role='driver',granted_roles='driver',current_line=line,is_on_line=True,driver_status='busy')
    db.add_all([passenger,driver]); db.flush()
    current=make_order(route,address_from=address_from,status='in_progress',driver_id=driver.id);current.passenger_id=passenger.id
    pending=make_order('улица Ленина',status='queued');pending.passenger_id=passenger.id
    other_line = 'Кусье' if line == 'Пашия' else 'Пашии'
    wrong=make_order(f'едем по {other_line}',status='queued');wrong.passenger_id=passenger.id
    db.add_all([current,pending,wrong]);db.flush()
    with patch.object(parallel,'available',return_value=[pending,wrong]), patch.object(parallel,'_remove_legacy_gorno_aggregate_refs'), patch.object(parallel,'_sync_parallel_notice') as notify:
        parallel.refresh_gornozavodsk_parallel_notices(db)
    args, kwargs=notify.call_args
    assert args[1].id == driver.id and args[2] == 1
    assert kwargs['eligible'] and '1 заявка' in kwargs['text']


def test_chat_notifications_roles_multiple_messages_and_reply(db):
    drivers=[User(vk_id=10,role='driver',granted_roles='passenger,driver',is_on_line=True,driver_status='online',is_blocked=False),
             User(vk_id=11,role='passenger',granted_roles='passenger,driver',is_on_line=True,driver_status='busy',is_blocked=False)]
    offline=User(vk_id=12,role='driver',granted_roles='driver',is_on_line=False,driver_status='offline',is_blocked=False)
    dispatcher=User(vk_id=13,role='dispatcher',granted_roles='dispatcher',is_on_line=True,driver_status='online',is_blocked=False)
    db.add_all(drivers+[offline,dispatcher]);db.flush()
    peer=2000000099
    seq=iter(range(100,200))
    def send(peer_id,text):
        row=OutboxMessage(peer_id=peer_id,text=text,random_id=next(seq),status='pending')
        db.add(row);db.flush();return row.id
    def message(sender, cmid, **extras):
        return dict(peer_id=peer,from_id=sender,conversation_message_id=cmid,id=cmid+1000,text='текст',**extras)
    with patch.object(chat,'get_int',return_value=peer), patch.object(chat.vk,'send_tracked_message',side_effect=send):
        chat.handle_message(db,message(99,1))
        chat.handle_message(db,message(98,2))
        chat.handle_message(db,message(99,1)) # duplicate VK event
        chat.handle_message(db,message(13,3)) # dispatcher ad
        db.flush()
        assert db.query(CommonChatNotice).count() == 2
        rows=db.query(OutboxMessage).order_by(OutboxMessage.id).all()
        assert [row.peer_id for row in rows] == [10,11,10,11]
        assert all(row.text == chat.NOTICE for row in rows)
        rows[0].status='sent';rows[0].last_error='vk_message_id:5'
        db.flush()
        chat.handle_message(db,message(10,4,reply_message={'conversation_message_id':1,'id':1001}))
        db.flush()
        assert [row.status for row in rows] == ['delete_requested','cancelled','pending','pending']
        notices=db.query(CommonChatNotice).order_by(CommonChatNotice.id).all()
        assert [n.answered for n in notices] == [True,False]
        chat.handle_message(db,message(13,5,reply_message={'conversation_message_id':2}))
        assert not notices[1].answered
        chat.handle_message(db,message(11,6,reply_message={'id':1002}))
        assert notices[1].answered
        assert rows[2].status == rows[3].status == 'cancelled'


def test_messages_in_same_chat_use_same_event_shard():
    def event(sender):
        return NS(type=VkBotEventType.MESSAGE_NEW,obj=NS(message={'from_id':sender,'peer_id':2000000001}))
    assert _event_actor_id(event(10)) == _event_actor_id(event(20)) == 2000000001
