"""Offline checks for common chat filtering, cancellations, pricing and VK polling."""
from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from common.models import Base, CommonChatNotice, OutboxMessage, User

with patch('vk_api.bot_longpoll.VkBotLongPoll.update_longpoll_server', return_value=None):
    from bot import common_chat_notices as chat

@pytest.fixture
def db():
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with sessionmaker(engine,autoflush=False,expire_on_commit=False)() as session:
        yield session
    engine.dispose()

@pytest.mark.parametrize('text', ['жду', 'жду на ул. Ленина 12', 'спасибо', 'ожидаю', 'привет', 'сколько стоит', 'какая цена?', 'почему так дорого', 'добрый вечер', 'отмена', 'уже не надо', 'машина приехала', 'водитель уже приехал'])
def test_followups_are_not_orders(text):
    assert not chat.is_ride_request(text)

@pytest.mark.parametrize('text', ['Нужно такси', 'машину пожалуйста', 'по Пашии', 'ПО КУСЬЕ', 'Пашия — Горнозаводск', 'из Пашии до Горнозаводска', 'ул Ленина 12', 'Ленина 12', 'нужно из Кусьи в город', 'хочу такси, сколько стоит?'])
def test_ride_requests_detected(text):
    assert chat.is_ride_request(text)

@pytest.mark.parametrize('text', ['сколько стоит', 'Сколько будет стоить?', 'какая цена', 'почему так дорого', 'а почему дорого?', 'стоимость поездки?', 'скажите тариф', 'почем поездка'])
def test_fare_questions(text):
    assert chat.is_price_question(text)

@pytest.mark.parametrize('text', ['привет', 'я жду', 'отмена', 'такси на Ленина', 'у меня 500 рублей', 'покажи дорогу'])
def test_not_fare_questions(text):
    assert not chat.is_price_question(text)


def test_chat_cancellation_only_author_and_reply(db):
    drivers=[User(vk_id=10,role='driver',granted_roles='driver',is_on_line=True,is_blocked=False),
             User(vk_id=11,role='driver',granted_roles='driver',is_on_line=True,is_blocked=False)]
    db.add_all(drivers);db.flush(); peer=2000000099; seq=iter(range(100,1000))
    def send(peer_id,text):
        row=OutboxMessage(peer_id=peer_id,text=text,random_id=next(seq),status='pending')
        db.add(row);db.flush();return row.id
    def message(sender,cmid,text,**extras):
        return dict(peer_id=peer,from_id=sender,conversation_message_id=cmid,id=1000+cmid,text=text,**extras)
    with patch.object(chat,'get_int',return_value=peer),patch.object(chat.vk,'send_tracked_message',side_effect=send):
        for i,t in enumerate(['жду', 'спасибо', 'привет', 'сколько стоит'], 1):
            chat.handle_message(db,message(90,i,t))
        assert db.query(CommonChatNotice).count() == 0
        chat.handle_message(db,message(90,10,'Нужно такси'))
        chat.handle_message(db,message(91,11,'Пашия — Кусья'))
        chat.handle_message(db,message(90,12,'такси на Ленина'))
        db.flush()
        notices=db.query(CommonChatNotice).order_by(CommonChatNotice.id).all()
        assert [n.author_vk_id for n in notices] == [90,91,90]
        chat.handle_message(db,message(91,13,'Отмена',reply_message={'conversation_message_id':10}))
        assert not any(n.answered for n in notices)
        chat.handle_message(db,message(90,14,'Уже не надо'))
        assert [n.answered for n in notices] == [False,False,True]
        chat.handle_message(db,message(10,15,'Хорошо',reply_message={'conversation_message_id':10}))
        assert [n.answered for n in notices] == [True,False,True]
        chat.handle_message(db,message(91,16,'Отмена'))
        assert all(n.answered for n in notices)
        assert all(row.status=='cancelled' for row in db.query(OutboxMessage).all())


def test_price_notices_are_admin_only_deduplicated_and_include_text(db):
    admin=User(vk_id=10,full_name='Админ',role='passenger',granted_roles='passenger,admin')
    other=User(vk_id=11,role='driver',granted_roles='driver')
    sender=User(vk_id=12,full_name='Иван Иванов',role='passenger',granted_roles='passenger')
    db.add_all([admin,other,sender]);db.flush()
    sent=[]
    with patch.object(chat,'get_int',return_value=2000000001), patch.object(chat.config,'ADMIN_VK_IDS',[10,13]), patch.object(chat.vk,'send_tracked_message',side_effect=lambda peer,text: sent.append((peer,text)) or len(sent)):
        chat.notify_price_question(db,dict(peer_id=12,from_id=12,text='Почему так дорого?'))
        assert [x[0] for x in sent] == [10,13]
        assert all('Иван Иванов' in x[1] and 'Почему так дорого?' in x[1] and 'личных сообщениях' in x[1] for x in sent)
        sent.clear()
        chat.notify_price_question(db,dict(peer_id=2000000001,from_id=12,text='Сколько стоит?'))
        assert [x[0] for x in sent] == [10,13]
        assert all('общем чате' in x[1] and chat.CHAT_LINK in x[1] for x in sent)
        sent.clear()
        chat.notify_price_question(db,dict(peer_id=2000000002,from_id=12,text='Сколько стоит?'))
        chat.notify_price_question(db,dict(peer_id=12,from_id=12,text='Привет'))
        assert sent == []


def test_deleted_polling_requires_visible_baseline_control_and_two_misses(db):
    db.add(OutboxMessage(peer_id=10,text='notice',random_id=55,status='pending'));db.flush()
    notice=CommonChatNotice(peer_id=2000000099,conversation_message_id=10,vk_message_id=110,author_vk_id=90,outbox_ids='[1]',answered=False)
    db.add(notice);db.flush()
    @contextmanager
    def scope():
        yield db
        db.flush()
    class Messages:
        found=[10,99]
        broken=False
        def getHistory(self,**kw):
            return {'items':[{'conversation_message_id':99}]}
        def getByConversationMessageId(self,**kw):
            if self.broken: raise RuntimeError('VK unavailable')
            return {'items':[{'conversation_message_id':i} for i in self.found]}
    api=NS(messages=Messages());chat._misses.clear()
    with patch.object(chat,'session_scope',scope),patch.object(chat.vk,'_message_api',return_value=api):
        chat.reconcile_deleted_messages(); assert notice.seen_in_vk and not notice.answered
        api.messages.broken=True
        chat.reconcile_deleted_messages(); assert not notice.answered
        api.messages.broken=False;api.messages.found=[]
        chat.reconcile_deleted_messages(); assert not notice.answered  # control unavailable
        api.messages.found=[99]
        chat.reconcile_deleted_messages(); assert not notice.answered
        chat.reconcile_deleted_messages(); assert notice.answered
        assert db.query(OutboxMessage).first().status=='cancelled'


def test_deleted_before_vk_baseline_is_not_assumed_deleted(db):
    notice=CommonChatNotice(peer_id=2000000099,conversation_message_id=10,author_vk_id=90,outbox_ids='[]',answered=False)
    db.add(notice);db.flush()
    @contextmanager
    def scope():
        yield db
        db.flush()
    api=NS(messages=NS(getHistory=lambda **kw:{'items':[{'conversation_message_id':99}]},getByConversationMessageId=lambda **kw:{'items':[{'conversation_message_id':99}]}))
    chat._misses.clear()
    with patch.object(chat,'session_scope',scope),patch.object(chat.vk,'_message_api',return_value=api):
        for _ in range(3): chat.reconcile_deleted_messages()
    assert not notice.answered
