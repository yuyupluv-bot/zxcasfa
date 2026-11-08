"""V181: real SQLite ORM + original functions; VK/FCM/timers are mocked.

This is NOT a live VK delivery or PostgreSQL concurrency test.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt
import json
import math
import re
import threading
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from common import models as m, time_utils
from bot import keyboards as kb
from bot.states_service import States, get_state, get_data, set_state, reset
from test_requirements_v173_requested_changes import load, relative_modules


class FixesV181(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        m.Base.metadata.create_all(self.engine)
        self.s = sessionmaker(self.engine, expire_on_commit=False, autoflush=False)()
        self.driver = m.User(vk_id=101, role='driver', granted_roles='driver,passenger',
                             full_name='Водитель', current_line='Горнозаводск', is_on_line=True,
                             driver_status='online', car_model='Москвич', car_color='Серый', car_number='А893АА')
        self.passenger = m.User(vk_id=201, full_name='Клиент')
        self.previous = m.User(vk_id=202, full_name='Предыдущий клиент')
        self.dispatcher = m.User(vk_id=301, role='dispatcher', granted_roles='dispatcher,passenger')
        self.other = m.User(vk_id=102, role='driver', granted_roles='driver,passenger')
        self.city = m.City(name='Горнозаводск', is_active=True)
        self.s.add_all([self.driver, self.passenger, self.previous, self.dispatcher, self.other, self.city]); self.s.flush()
        self.s.add_all([m.DriverQueue(driver_id=self.driver.id, city_id=self.city.id, position=1, status='waiting'),
                       m.DriverQueue(driver_id=self.other.id, city_id=self.city.id, position=2, status='waiting')]); self.s.flush()
        self.vk = Mock()
        self.vk.send_message.return_value = True
        self.vk.send_tracked_message.side_effect = self.tracked
        self.vk.prepare_voice_attachment.side_effect = lambda peer, source: f'doc-77_{peer}' if source else None
        self.vk.reupload_attachments.return_value = ['doc-77_5']
        self.timers = Mock()
        self.settings = {'driver_fallback_chat_peer_id': 2000000001, 'driver_accept_timeout': 60}
        self.outbox = types.ModuleType('v173_runtime.outbox_service')
        self.outbox_scope = self.scope()
        for name in ('_stored_vk_marker_id', '_stored_vk_message_id', '_stored_vk_cmid', 'has_usable_vk_id', 'finalize_tracked_message'):
            load('bot/outbox_service.py', name, self.outbox_scope)
        self.outbox.finalize_tracked_message = self.outbox_scope['finalize_tracked_message']
        self.outbox.cancel_or_delete = Mock(return_value=True)
        self.q = self.scope()
        self.q.update(_sync_away_notice=Mock(), _notify_fronts=Mock())
        for name in ('_min_position', 'release_offer', 'mark_offered', 'move_to_front'):
            load('bot/queue_service.py', name, self.q)
        self.queue = NS(**{name:self.q[name] for name in ('release_offer', 'mark_offered', 'move_to_front')})
        self.queue.set_away = Mock(); self.queue.leave_queue = Mock()
        self.os = self.scope()
        self.os["DELIVERY_TYPES"] = ("delivery", "partner_delivery")
        self.os.update(queue_service=self.queue, _set_state=set_state,
                       CHAT_UNCLAIMED_TIMEOUT_SECONDS=2700,
                       send_fallback_chat_tracked_notice=lambda session,text,**kw:self.vk.send_tracked_message(2000000001,text,**kw))
        for name in ('order_text', 'is_delivery_type', '_passenger_review_word', 'driver_rating_text',
                     'departure_driver_card_text', 'normalize_driver_chat_peer_id', '_declined_ids', '_add_declined',
                     '_declined_reasons', 'add_decline', '_set_current_offer', '_current_offer', 'clear_current_offer',
                     'finalize_offer_message', 'publish_special_decline_to_requests_chat', 'start_free_waiting'):
            load('bot/order_service.py', name, self.os)
        self.service = NS(**{k:v for k,v in self.os.items() if callable(v)})
        self.service.multi_car_label = Mock(return_value=None)
        self.service.schedule_prearrival_notice = Mock()
        self.service.offer_to_next_driver = Mock()
        self.service._accept_timeout = Mock()
        self.msg = self.scope()
        for name in ('_counterpart', 'relay'):
            load('bot/messaging.py', name, self.msg)
        self.h = self.scope()
        self.h.update(order_service=self.service, queue_service=self.queue, relay=self.msg['relay'],
                      show_main_menu=Mock(), _driver_ride_kb=Mock(return_value='ride'),
                      _restore_active_menu_after_stale_offer=Mock(return_value=False),
                      can_switch_role=lambda user:False, _passenger_labels=lambda session:{},
                      passenger_queue=Mock(), customer_price_service=NS(has_price=lambda o:o.customer_price is not None),
                      passenger_cancel=Mock(), passenger_cancel_request=Mock(),
                      delivery_service=NS(is_delivery=lambda o:False,is_partner_delivery=lambda o:False),
                      _is_dispatcher_order=lambda o: bool(o.dispatcher_id))
        for name in ('active_order_for','passenger_order_for','driver_chat_order_for','enter_chat','exit_chat','chat_forward',
                     'dispatcher_reply_start','dispatcher_reply_send','passenger_departure_response','_apply_eta','driver_decline'):
            load('bot/handlers.py',name,self.h)

    def scope(self):
        result = {k:getattr(m,k) for k in ('User','Order','DriverQueue','City','OutboxMessage','Message','DispatcherCommission')}
        result.update(__package__='v173_runtime', re=re,json=json,math=math,dt=dt,func=func,
                      vk=self.vk,timers=self.timers,kb=kb,time_utils=time_utils,States=States,
                      get_state=get_state,get_data=get_data,set_state=set_state,reset=reset,
                      ROLE_DISPATCHER='dispatcher',log=Mock(), audit=Mock(),
                      get_int=lambda session,key,default=0:self.settings.get(key,default),
                      get_float=lambda session,key,default=0:default,waiting_service=Mock())
        return result

    def tearDown(self):
        self.s.close();self.engine.dispose()

    def tracked(self, peer, text, **kw):
        row=m.OutboxMessage(peer_id=peer,text=text,keyboard=kw.get('keyboard'),attachment=kw.get('attachment'),
                            status='pending',random_id=self.s.query(m.OutboxMessage).count()+1)
        self.s.add(row);self.s.flush();return row.id

    def order(self, **kw):
        values=dict(passenger_id=self.passenger.id,address_from='Новая 8',address_to='Ленина 2',
                    route_text='Новая 8 — Ленина 2',status='assigned',driver_id=self.driver.id,public_number=3000)
        values.update(kw);o=m.Order(**values);self.s.add(o);self.s.flush();return o

    def current_and_parallel(self):
        current=self.order(passenger_id=self.previous.id,status='in_progress',public_number=2999)
        parallel=self.order(driver_id=None,parallel_driver_id=self.driver.id,status='parallel_assigned',customer_price=350)
        return current,parallel

    def test_parallel_chat_both_directions_use_reserved_order_not_current(self):
        current,parallel=self.current_and_parallel()
        self.assertTrue(self.msg['relay'](self.s,parallel,self.passenger,'Встречайте',[]))
        self.assertEqual(get_state(self.s,101).state,States.D_CHAT)
        self.assertEqual(get_data(self.s,101)['order_id'],parallel.id)
        self.h['chat_forward'](self.s,self.driver,'Уже еду',[],True)
        sent=self.vk.send_message.call_args_list
        self.assertTrue(any(c.kwargs.get('peer_id')==201 and 'Уже еду' in c.kwargs.get('text','') for c in sent))
        self.assertFalse(any(c.kwargs.get('peer_id')==202 for c in sent))
        self.s.flush();self.assertEqual({x.order_id for x in self.s.query(m.Message)}, {parallel.id})

    def test_chat_rejects_outsider_closed_order_and_stale_driver_binding(self):
        current,parallel=self.current_and_parallel()
        self.assertFalse(self.msg['relay'](self.s,parallel,self.other,'Чужой',[]))
        parallel.status='completed';self.s.flush()
        self.assertFalse(self.msg['relay'](self.s,parallel,self.driver,'Устарело',[]))
        self.assertIsNone(self.h['driver_chat_order_for'](self.s,self.driver,parallel.id))
        self.assertIsNone(self.h['driver_chat_order_for'](self.s,self.other,current.id))
        self.assertIsNone(self.h['driver_chat_order_for'](self.s,self.driver,'bad'))

    def test_parallel_chat_does_not_overwrite_fare_input(self):
        _,parallel=self.current_and_parallel()
        set_state(self.s,101,States.D_FINISH_PRICE,{'order_id':1})
        self.msg['relay'](self.s,parallel,self.passenger,'Добрый день',[])
        self.assertEqual(get_state(self.s,101).state,States.D_FINISH_PRICE)

    def test_dispatcher_voice_reply_reaches_parallel_driver(self):
        _,parallel=self.current_and_parallel();parallel.passenger_id=self.dispatcher.id;parallel.dispatcher_id=self.dispatcher.id
        self.s.flush()
        self.h['dispatcher_reply_start'](self.s,self.dispatcher,parallel.id)
        voice=[{'type':'audio_message','audio_message':{'owner_id':301,'id':5,'link_ogg':'https://vk.example/voice.ogg'}}]
        self.h['dispatcher_reply_send'](self.s,self.dispatcher,'',voice)
        self.vk.reupload_attachments.assert_called_once_with(101,voice)
        self.assertEqual(self.vk.send_message.call_args_list[-2].kwargs['attachment'],'doc-77_5')
        self.assertEqual(self.vk.send_message.call_args_list[-2].kwargs['peer_id'],101)

    def test_eta_is_one_card_yes_edits_it_and_is_idempotent(self):
        o=self.order()
        self.h['_apply_eta'](self.s,self.driver,o,5)
        self.vk.send_tracked_message.assert_called_once()
        row=self.s.get(m.OutboxMessage,o.departure_prompt_outbox_id)
        for text in ('Водитель','Москвич','А893АА','через 5 мин.','Ждёте машинку?'):self.assertIn(text,row.text)
        self.assertFalse(any(c.args and c.args[0]==201 for c in self.vk.send_message.call_args_list))
        with relative_modules(outbox_service=self.outbox):
            self.h['passenger_departure_response'](self.s,self.passenger,False,o.id)
            self.h['passenger_departure_response'](self.s,self.passenger,False,o.id)
        self.assertIn('Машинка в пути',row.text);self.assertNotIn('Ждёте',row.text)
        self.assertIn('А893АА',row.text);self.assertEqual(json.loads(row.keyboard)['buttons'],[])
        self.assertTrue(o.actuality_confirmed);self.assertIsNone(o.departure_prompt_outbox_id)
        self.assertEqual(sum(c.args==(101,'Клиент ожидает вас на месте') for c in self.vk.send_message.call_args_list),1)

    def test_no_cancels_directly_and_clears_exact_card(self):
        o=self.order();o.departure_prompt_outbox_id=self.tracked(201,'Водитель\nАвто А893АА\nЖдёте машинку?',keyboard=kb.passenger_departure_keyboard(o.id))
        row=self.s.get(m.OutboxMessage,o.departure_prompt_outbox_id)
        with relative_modules(outbox_service=self.outbox):self.h['passenger_departure_response'](self.s,self.passenger,True,o.id)
        self.h['passenger_cancel'].assert_called_once_with(self.s,self.passenger,o.id)
        self.h['passenger_cancel_request'].assert_not_called()
        self.assertIn('Заявка отменена',row.text);self.assertNotIn('Ждёте',row.text)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])

    def test_arrival_strips_unanswered_question_but_keeps_car(self):
        o=self.order();o.departure_prompt_outbox_id=self.tracked(201,'Водитель\nАвто А893АА\nЖдёте машинку?',keyboard=kb.passenger_departure_keyboard(o.id))
        row=self.s.get(m.OutboxMessage,o.departure_prompt_outbox_id)
        self.os['_is_dispatcher_order']=lambda o:False
        with relative_modules(outbox_service=self.outbox):self.os['start_free_waiting'](self.s,o)
        self.assertIn('А893АА',row.text);self.assertNotIn('Ждёте',row.text)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])

    def test_promotion_contains_customer_price_and_preserves_voice_source(self):
        for amount in (350,0,None):
            with self.subTest(amount=amount):
                self.s.query(m.Order).delete();self.s.flush()
                _,o=self.current_and_parallel();o.customer_price=amount
                o.voice_attachment=json.dumps({'vk_voice':{'link_ogg':'https://vk.example/source.ogg'}})
                source=o.voice_attachment
                self.s.query(m.Order).filter(m.Order.id!=o.id).update({'status':'completed'});self.s.flush()
                p=self.scope();p.update(ACTIVE=('assigned','arrived','in_progress'))
                handlers=types.ModuleType('v173_runtime.handlers');handlers._driver_ride_kb=self.h['_driver_ride_kb']
                with relative_modules(order_service=self.service,handlers=handlers):load('bot/parallel_orders.py','promote_after_current',p)(self.s,self.driver)
                text=self.vk.send_message.call_args_list[-2].args[1]
                self.assertIn('#3000',text)
                self.assertEqual('Цена клиента:' in text,amount is not None)
                if amount is not None:self.assertIn(f'{amount} ₽',text)
                self.assertEqual(o.voice_attachment,source);self.assertEqual(o.driver_id,self.driver.id)
                self.assertIsNone(o.parallel_driver_id)

    def handoff(self, no_chat=False, personal=False):
        o=self.order(status='searching',driver_id=None,offered_driver_id=self.driver.id,
                     requested_driver_id=self.driver.id if personal else None,customer_price=350)
        self.s.query(m.DriverQueue).filter_by(driver_id=self.driver.id).one().status='offered';self.s.flush()
        if no_chat:self.settings['driver_fallback_chat_peer_id']=0
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.local_village_line=lambda order:None # this handoff is not a local village trip
        with relative_modules(outbox_service=self.outbox,parallel_orders=parallel):self.h['driver_decline'](self.s,self.driver,o.id,'to_driver_chat')
        return o

    def test_handoff_mentions_all_keeps_first_and_has_exact_passenger_notice(self):
        o=self.handoff()
        self.assertEqual(o.status,'chat_search');self.assertIsNone(o.offered_driver_id)
        card=self.s.get(m.OutboxMessage,o.chat_notice_outbox_id)
        self.assertTrue(card.text.startswith('@all\n'));self.assertIn('350 ₽',card.text)
        self.assertIn(self.driver.id,json.loads(o.declined_driver_ids))
        rows=self.s.query(m.DriverQueue).order_by(m.DriverQueue.position).all()
        self.assertEqual(rows[0].driver_id,self.driver.id);self.assertEqual(rows[0].status,'waiting')
        self.assertTrue(self.driver.is_on_line)
        self.assertTrue(any(c.args[:2]==(201,'✅ Активно ищем водителя в чате, ожидайте') for c in self.vk.send_message.call_args_list))
        self.service.offer_to_next_driver.assert_not_called()
        before=self.vk.send_tracked_message.call_count
        self.h['driver_decline'](self.s,self.driver,o.id,'to_driver_chat')
        self.assertEqual(self.vk.send_tracked_message.call_count,before)

    def test_failed_handoff_restores_offer_not_false_success(self):
        o=self.handoff(no_chat=True)
        self.assertEqual(o.status,'searching');self.assertEqual(o.offered_driver_id,self.driver.id)
        self.assertFalse(o.declined_driver_ids)
        self.assertEqual(self.s.query(m.DriverQueue).filter_by(driver_id=self.driver.id).one().status,'offered')
        self.assertFalse(any(c.args[:2]==(201,'✅ Активно ищем водителя в чате, ожидайте') for c in self.vk.send_message.call_args_list))

    def test_personal_refusal_gives_choose_another_notice_and_never_leaks(self):
        o=self.handoff(personal=True)
        self.assertEqual(o.status,'cancelled');self.assertIsNone(o.chat_notice_outbox_id)
        self.assertTrue(any(c.args[:2]==(201,'Данный водитель не может взять заказ. Попробуйте выбрать другого водителя.') for c in self.vk.send_message.call_args_list))

    def test_buttons_preserve_old_reasons_and_use_yes_wait(self):
        data=json.loads(kb.decline_reasons_keyboard(17,True))
        payloads=[json.loads(b['action']['payload']) for row in data['buttons'] for b in row]
        self.assertEqual({p.get('cat') for p in payloads if p['cmd']=='decline_reason'},
                         {'far','delivery','booking','away','need_address','spam','dislike','low_price','to_driver_chat'})
        self.assertNotIn('to_driver_chat',kb.decline_reasons_keyboard(17,True,allow_chat_handoff=False))
        labels=[b['action']['label'] for row in json.loads(kb.parallel_actuality_keyboard(17))['buttons'] for b in row]
        self.assertEqual(labels,['✅ Да, жду','❌ Нет, отменяю заявку'])

    def test_final_summary_uses_public_number_not_internal_id(self):
        o=self.order(id=10500,public_number=3000,status='in_progress')
        h=self.h.copy();h.update(_parse_price=lambda text:350,_price_in_range=lambda p:True,
            waiting_service=NS(finalize=lambda *a:(0,0)),booking_service=Mock(),night_tariff=Mock(),
            _leave_temporary_chat_line=Mock(),_ask_driver_rate_passenger=Mock(),lines=Mock(),
            bm=NS(render=lambda *a,**kw:('Оцените поездку',None)))
        parallel=types.ModuleType('v173_runtime.parallel_orders');parallel.release_route_offers_for_trip=Mock();parallel.has_pending=lambda *a:False
        expiry=types.ModuleType('v173_runtime.rating_expiry_service');expiry.schedule_expiry=Mock()
        with relative_modules(parallel_orders=parallel,rating_expiry_service=expiry):load('bot/handlers.py','driver_complete_ride',h)(self.s,self.driver,'350')
        texts=[c.args[1] for c in self.vk.send_message.call_args_list if len(c.args)>1]
        summary=next(t for t in texts if t.startswith('🧾 Итог'))
        self.assertIn('#3000',summary);self.assertNotIn('10500',summary)


class VoiceSources(unittest.TestCase):
    def client(self):
        scope={'json':json,'re':re,'log':Mock(),'config':NS(VK_GROUP_ID=77)}
        cls=load('bot/vk_client.py','VKClient',scope);c=object.__new__(cls)
        c._api_lock=threading.RLock();c.api=Mock();c._reupload_voice=Mock(side_effect=lambda peer,voice:f'doc-77_{peer}')
        return c

    def test_forwarded_dispatcher_voice_is_extracted_and_reuploaded_for_each_peer(self):
        c=self.client();voice={'owner_id':301,'id':9,'link_ogg':'https://vk.example/voice.ogg'}
        atts=c.message_attachments({'fwd_messages':[{'attachments':[{'type':'audio_message','audio_message':voice}]}]})
        source=c.voice_attachment_reference(atts)
        self.assertEqual(c.prepare_voice_attachment(101,source),'doc-77_101')
        self.assertEqual(c.prepare_voice_attachment(102,source),'doc-77_102')
        self.assertEqual(json.loads(source)['vk_voice'],voice)
        self.assertEqual(c._reupload_voice.call_count,2)

    def test_legacy_private_voice_is_refreshed_before_forwarding(self):
        c=self.client();voice={'owner_id':301,'id':9,'link_ogg':'https://vk.example/voice.ogg'}
        c.api.docs.getById.return_value=[voice]
        self.assertEqual(c.prepare_voice_attachment(101,'doc301_9_access'),'doc-77_101')
        c.api.docs.getById.assert_called_once_with(docs='301_9_access')



class ImportedFlowTests(unittest.TestCase):
    """Import the complete bot dependency graph; hard-block all HTTP calls."""
    scope = FixesV181.scope
    tracked = FixesV181.tracked
    order = FixesV181.order
    current_and_parallel = FixesV181.current_and_parallel

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.network_guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled in tests'))
        cls.network_guard.start()
        cls.longpoll_guard = patch('vk_api.bot_longpoll.VkBotLongPoll')
        cls.longpoll_guard.start()
        try:
            cls.handlers = importlib.import_module('bot.handlers')
            cls.parallel = importlib.import_module('bot.parallel_orders')
            cls.orders = importlib.import_module('bot.order_service')
            cls.queue = importlib.import_module('bot.queue_service')
            importlib.import_module('bot.push_service')
            cls.settings_module = importlib.import_module('common.settings_service')
        except Exception:
            cls.longpoll_guard.stop();cls.network_guard.stop();raise

    @classmethod
    def tearDownClass(cls):
        cls.longpoll_guard.stop();cls.network_guard.stop()

    def setUp(self):
        FixesV181.setUp(self)
        self.queue = type(self).queue
        self.patches=[]
        for name,module in list(sys.modules.items()):
            if name.startswith('bot.') and isinstance(module,types.ModuleType) and hasattr(module,'vk'):
                self.patches.append(patch.object(module,'vk',self.vk))
        for name in ('schedule','cancel','cancel_all_for_order'):
            self.patches.append(patch.object(sys.modules['bot.timers'],name,Mock()))
        for name in ('_notify_fronts','_sync_away_notice'):
            self.patches.append(patch.object(self.queue,name,Mock()))
        self.patches.append(patch.object(sys.modules['bot.push_service'],'send',Mock()))
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.settings_module._cache.clear()
        self.s.add(m.Setting(key='driver_fallback_chat_peer_id',value='2000000001'));self.s.flush()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.settings_module._cache.clear()
        FixesV181.tearDown(self)

    def test_actual_passenger_and_driver_handlers_relay_parallel_voice(self):
        current,parallel=self.current_and_parallel()
        self.handlers.handle_passenger(self.s,self.passenger,States.P_WAITING,'Сообщение',{},[])
        self.assertEqual(get_state(self.s,101).state,States.D_CHAT)
        self.assertEqual(get_data(self.s,101)['order_id'],parallel.id)
        voice=[{'type':'audio_message','audio_message':{'link_ogg':'https://vk.example/voice.ogg'}}]
        self.handlers.handle_driver(self.s,self.driver,States.D_CHAT,'',{},voice)
        self.vk.reupload_attachments.assert_called_with(201,voice)
        self.assertEqual(self.vk.send_message.call_args_list[-2].kwargs['peer_id'],201)

    def test_actual_dispatcher_voice_creation_keeps_source_and_delivers_offer(self):
        voice=[{'type':'audio_message','audio_message':{'owner_id':301,'id':9,'link_ogg':'https://vk.example/voice.ogg'}}]
        source=json.dumps({'vk_voice':voice[0]['audio_message']},ensure_ascii=False)
        self.vk.voice_attachment_reference.return_value=source
        set_state(self.s,301,States.P_ADDR,{'draft':{'dispatcher':True}},merge=False)
        self.handlers.handle_dispatcher(self.s,self.dispatcher,States.P_ADDR,'',{},voice)
        order=self.s.query(m.Order).one()
        self.assertEqual(order.dispatcher_id,self.dispatcher.id)
        self.assertEqual(order.voice_attachment,source)
        self.assertEqual(order.offered_driver_id,self.driver.id)
        self.assertTrue(any(c.kwargs.get('attachment')=='doc-77_101' for c in self.vk.send_tracked_message.call_args_list))
        # A reassignment uploads for the next recipient without losing the source.
        self.queue.release_offer(self.s,self.driver)
        self.orders.add_decline(self.s,order,self.driver.id,'dislike')
        self.orders.clear_current_offer(self.s,order);self.s.flush()
        self.other.is_on_line=True;self.other.driver_status='online';self.s.flush()
        self.orders.offer_to_next_driver(self.s,order)
        self.assertEqual(order.voice_attachment,source)
        self.assertTrue(any(c.kwargs.get('attachment')=='doc-77_102' for c in self.vk.send_tracked_message.call_args_list))

    def test_actual_handoff_keeps_front_and_queues_all_card(self):
        order=self.order(status='searching',driver_id=None,offered_driver_id=self.driver.id)
        self.s.query(m.DriverQueue).filter_by(driver_id=self.driver.id).one().status='offered';self.s.flush()
        self.handlers.driver_decline(self.s,self.driver,order.id,'to_driver_chat')
        self.assertEqual(order.status,'chat_search')
        self.assertEqual(self.s.query(m.DriverQueue).order_by(m.DriverQueue.position).first().driver_id,self.driver.id)
        self.assertTrue(self.s.get(m.OutboxMessage,order.chat_notice_outbox_id).text.startswith('@all'))
        self.assertTrue(any(c.args[:2]==(201,'✅ Активно ищем водителя в чате, ожидайте') for c in self.vk.send_message.call_args_list))

    def test_actual_eta_yes_updates_same_card(self):
        order=self.order()
        self.handlers._apply_eta(self.s,self.driver,order,5)
        row=self.s.get(m.OutboxMessage,order.departure_prompt_outbox_id)
        self.handlers.passenger_departure_response(self.s,self.passenger,False,order.id)
        self.assertIn('Машинка в пути',row.text)
        self.assertIn('А893АА',row.text)
        self.assertNotIn('Ждёте',row.text)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])

    def test_actual_parallel_chat_button_binds_to_next_passenger(self):
        current,parallel=self.current_and_parallel()
        data=json.loads(kb.parallel_reserved_keyboard(parallel.id))
        buttons=[json.loads(b['action']['payload']) for row in data['buttons'] for b in row]
        payload=next(b for b in buttons if b['cmd']=='chat')
        self.handlers.handle_driver(self.s,self.driver,States.D_IN_RIDE,'',payload,[])
        self.assertEqual(get_data(self.s,101)['order_id'],parallel.id)
        self.handlers.handle_driver(self.s,self.driver,States.D_CHAT,'Здравствуйте',{},[])
        self.assertEqual(self.vk.send_message.call_args_list[-2].kwargs['peer_id'],201)

    def test_actual_eta_no_cancels_without_second_confirmation(self):
        order=self.order()
        self.handlers._apply_eta(self.s,self.driver,order,5)
        row=self.s.get(m.OutboxMessage,order.departure_prompt_outbox_id)
        self.handlers.passenger_departure_response(self.s,self.passenger,True,order.id)
        self.assertEqual(order.status,'cancelled')
        self.assertIn('Заявка отменена',row.text)
        self.assertNotIn('Ждёте',row.text)
        self.assertEqual(json.loads(row.keyboard)['buttons'],[])

    def test_actual_finish_and_parallel_transition_use_public_number_and_customer_price(self):
        current,parallel=self.current_and_parallel()
        current.public_number=2999;current.id=10500;self.s.flush()
        self.handlers.driver_complete_ride(self.s,self.driver,'350')
        texts=[c.args[1] for c in self.vk.send_message.call_args_list if len(c.args)>1]
        self.assertTrue(any('🧾 Итог по поездке #2999:' in text for text in texts))
        transition=next(text for text in texts if text.startswith('Переходим к заявке #3000:'))
        self.assertIn('Цена клиента: 350 ₽',transition)
        self.assertEqual(parallel.driver_id,self.driver.id)
        self.assertEqual(parallel.status,'assigned')


if __name__=='__main__':unittest.main()
