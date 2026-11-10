"""V176: dispatch outcome, delivery identity, per-line FIFO and VK menus."""
from __future__ import annotations
import ast
import datetime as dt
import json
import math
import re
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load, node, relative_modules, source
import test_requirements_v173_requested_changes as previous


def keyboard_scope():
    scope={'json':json,'math':math}
    constants=[n for n in ast.parse(source('bot/keyboards.py')).body
               if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant)]
    exec(compile(ast.Module(body=constants,type_ignores=[]),'keyboard constants','exec'),scope)
    for name in ('fit_label','_btn','keyboard','passenger_ride_keyboard','passenger_departure_keyboard'):
        load('bot/keyboards.py',name,scope)
    return scope

class DispatchOutcomeV176(unittest.TestCase):
    def dispatch(self,local=False,busy=False,fallback_driver=None,dispatcher=False):
        order=NS(id=7,passenger_id=2,dispatcher_id=9 if dispatcher else None,
                 status='created',offered_driver_id=None,driver_id=None)
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.route_priority_city=lambda o:'Кусья'
        parallel.free_line_city=lambda o:'Кусья'
        parallel.notify_busy_drivers=Mock(return_value=busy)
        history=[]
        def fallback(*args,**kw):
            history.append('fallback')
            if fallback_driver:
                order.offered_driver_id=fallback_driver;order.status='searching'
        parallel._fallback_to_free_drivers=Mock(side_effect=fallback)
        def offer(*args,**kw):
            order.offered_driver_id=1;order.status='searching';return True
        service=NS(offer_to_next_driver=Mock(side_effect=offer))
        def enqueue(*args):order.status='queued'
        vk=Mock();vk.send_message.side_effect=lambda *a,**kw:history.append(a[1])
        session=Mock();session.get.return_value=NS(vk_id=22)
        scope={'__package__':'v173_runtime','User':object,
               'queue_service':NS(has_waiting_driver=Mock(return_value=local)),
               'order_service':service,'enqueue':Mock(side_effect=enqueue),'vk':vk,'kb':Mock()}
        with relative_modules(parallel_orders=parallel):
            load('bot/passenger_queue.py','dispatch_new_order',scope)(session,order)
        return order,parallel,service,vk,history

    def test_kusya_empty_but_gorno_free_says_minute_after_real_fallback(self):
        order,parallel,service,vk,history=self.dispatch(fallback_driver=3)
        self.assertEqual(history,['fallback','⏰ Минутку…'])
        self.assertEqual(order.offered_driver_id,3)
        parallel._fallback_to_free_drivers.assert_called_once()

    def test_no_eligible_free_driver_says_wait(self):
        *_,vk,history=self.dispatch()
        self.assertEqual(history,['fallback','⏰ Чуть подождать, первый свободный, возьмет вашу заявку…'])
        vk.send_message.assert_called_once()

    def test_local_line_precedes_busy_and_gorno(self):
        _,parallel,service,vk,history=self.dispatch(local=True,busy=True,fallback_driver=3)
        parallel.notify_busy_drivers.assert_not_called()
        parallel._fallback_to_free_drivers.assert_not_called()
        self.assertEqual(history,['⏰ Минутку…'])
        self.assertEqual(service.offer_to_next_driver.call_args.kwargs,{'line_scope':'exact','line_name':'Кусья'})

    def test_real_busy_route_offer_still_precedes_gorno(self):
        _,parallel,_,_,history=self.dispatch(busy=True,fallback_driver=3)
        parallel._fallback_to_free_drivers.assert_not_called()
        self.assertEqual(history,['⏰ Чуть подождать, первый свободный, возьмет вашу заявку…'])

    def test_dispatcher_gets_no_passenger_waiting_notice(self):
        *_,vk,history=self.dispatch(fallback_driver=3,dispatcher=True)
        vk.send_message.assert_not_called()
        self.assertEqual(history,['fallback'])

    def test_decline_fallback_to_free_does_not_send_false_wait(self):
        order=NS(id=7,number=1650,passenger_id=2,dispatcher_id=None,status='created',
                 offered_driver_id=None,driver_id=None)
        session=Mock();session.get.return_value=NS(vk_id=22)
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.route_priority_city=lambda o:'Кусья'
        parallel.local_village_line=lambda o:None # this fixture is not a local trip
        parallel.notify_busy_drivers=lambda *a:False
        def fallback(*a,**kw):order.offered_driver_id=3;order.status='searching'
        parallel._fallback_to_free_drivers=fallback
        passenger_queue=types.ModuleType('v173_runtime.passenger_queue');passenger_queue.enqueue=Mock()
        scope={'__package__':'v173_runtime','User':object,'has_eligible_waiting_driver':lambda *a:False,
               'clear_offer_notice':Mock(),'_declined_reasons':lambda *a:[],
               '_is_dispatcher_order':lambda o:False,'is_delivery_type':lambda o:False,
               'vk':Mock(),'kb':Mock()}
        with relative_modules(parallel_orders=parallel,passenger_queue=passenger_queue):
            load('bot/order_service.py','_handle_no_driver',scope)(session,order)
        self.assertEqual(scope['vk'].send_message.call_args.args[1],'⏰ Минутку…')

    def test_chat_fallback_has_no_all_refused_summary(self):
        order=NS(id=7,number=1650,passenger_id=2,dispatcher_id=None,status='created',voice_attachment=None)
        session=Mock();session.get.return_value=NS(vk_id=22,full_name='Клиент')
        scope={'User':object,'has_eligible_waiting_driver':lambda *a:False,'clear_offer_notice':Mock(),
               '_declined_reasons':lambda *a:[],'_is_dispatcher_order':lambda o:False,
               'is_delivery_type':lambda o:True,'get_int':lambda *a:2000000001,
               'normalize_driver_chat_peer_id':lambda value:value,'send_fallback_chat_tracked_notice':Mock(return_value=77),
               'order_text':lambda o:'Доставка пакета','vk':Mock(),'kb':Mock(),'timers':Mock(),
               'CHAT_UNCLAIMED_TIMEOUT_SECONDS':2700,'_driver_chat_timeout':Mock()}
        scope['__package__']='v173_runtime'
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.local_village_line=lambda o:None # regular delivery, not local
        with relative_modules(parallel_orders=parallel):
            load('bot/order_service.py','_handle_no_driver',scope)(session,order)
        self.assertEqual([c.args[1] for c in scope['vk'].send_message.call_args_list],
                         ['Заявка отправлена в чат заявок. Ожидаем, когда водитель возьмёт её.'])
        self.assertEqual(order.status,'chat_search')
        self.assertEqual(scope['timers'].schedule.call_args.args[2],2700)

class LineGroupsV176(unittest.TestCase):
    def render(self,items,dispatcher=False):
        scope={'format_rating':lambda d:'⭐ 5.0 (7 отзывов)',
               '_DISPLAY_ORIGIN_LABELS':{'Пашия':'из Пашии'},
               '_DISPLAY_DESTINATION_LABELS':{'Пашия':'до Пашии'},
               '_display_route_direction':lambda o:(None,'Пашия') if o else (None,None)}
        fn=load('bot/handlers.py','_render_driver_statuses',scope)
        return fn(items,current_driver_id=2,numbered=True,group_free_by_line=True,
                  show_passenger_details=dispatcher)

    def fixture(self):
        items=[]
        for id,line,status in ((1,'Горнозаводск','online'),(2,'Пашия','online'),(3,'Кусья','online'),
                               (4,'Горнозаводск','online'),(5,'Пашия','busy'),(6,'Пашия','considering')):
            driver=NS(id=id,vk_id=id+100,full_name=f'Водитель {id}',current_line='НЕВЕРНАЯ ЛИНИЯ')
            items.append({'driver':driver,'status':status,'position':id,'line_name':line,
                          'order':NS() if status=='busy' else None,'passenger_label':'Клиент','order_id':1650})
        return items

    def test_each_line_starts_at_one_and_gorno_second_is_two(self):
        text='\n'.join(self.render(self.fixture()))
        self.assertIn('1. 🟢 Водитель 1 — свободен — Горнозаводск',text)
        self.assertIn('2. 🟢 Водитель 4',text)
        self.assertIn('1. 🟢 Водитель 2 (вы) — свободен — Пашия',text)
        self.assertIn('1. 🟢 Водитель 3 — свободен — Кусья',text)
        self.assertNotIn('НЕВЕРНАЯ ЛИНИЯ',text)

    def test_nonfree_drivers_are_not_numbered_as_free(self):
        text='\n'.join(self.render(self.fixture()))
        self.assertIn('Другие статусы:',text)
        self.assertIn('🟠 Водитель 6 — рассматривает заявку',text)
        self.assertIn('На заявке до Пашии:',text)
        self.assertNotRegex(text,r'\d+\. [🔴🟠]')
        self.assertNotIn('Клиент',text)

    def test_dispatcher_only_sees_passenger_details(self):
        text='\n'.join(self.render(self.fixture(),dispatcher=True))
        self.assertIn('Клиент, заявка #1650',text)

    def test_queue_header_and_groups_use_same_canonical_line_snapshot(self):
        screen=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','show_queue'))
        self.assertIn('driver_line_rank(session, user, entries=entries)',screen)
        self.assertIn('group_free_by_line=True',screen)
        self.assertIn('city_names.get(item["city_id"]',screen)
        self.assertEqual(screen.count('queue_service.queue_entries(session)'),1)

class EtaAndMenuV176(unittest.TestCase):
    def test_delivery_receives_identity_car_eta_and_persistent_menu(self):
        order,vk,service=previous.MessageChangesV173().eta(delivery=True)
        first=vk.send_message.call_args_list[0]
        self.assertIn('[id11|Водитель]',first.args[1])
        self.assertIn('Авто: Москвич 3, Серый, 893',first.args[1])
        self.assertIn('доставку примерно за 2 мин.',first.args[1])
        self.assertIn('keyboard',first.kwargs)
        vk.send_tracked_message.assert_not_called()
        self.assertIn('Купил в магазине',vk.send_message.call_args_list[-1].args[1])

    def test_parallel_eta_also_has_link_rating_car_and_main_menu(self):
        order=NS(id=7,number=1650,parallel_driver_id=1,status='parallel_assigned',passenger_id=2,
                 route_text='Кусья до города',dispatcher_id=None)
        driver=NS(id=1,vk_id=11,full_name='Имя',car_full='Машина')
        session=Mock()
        session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
        session.query.return_value.filter.return_value.first.return_value=None
        session.get.return_value=NS(vk_id=22,full_name='Клиент')
        orders=types.ModuleType('v173_runtime.order_service')
        orders.finalize_offer_message=Mock()
        orders.departure_driver_card_text=Mock(return_value='[id11|Имя] 5.0 ⭐ (7 отзывов)\nАвто: Машина')
        scope={'__package__':'v173_runtime','Order':NS(id=0,driver_id=0,status=Mock()),'User':object,
               'timers':Mock(),'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)),
               'vk':Mock(),'kb':Mock(),'ACTIVE':('assigned','arrived','in_progress'),'set_state':Mock()}
        with relative_modules(order_service=orders):
            load('bot/parallel_orders.py','save_eta',scope)(session,driver,7,5)
        text=scope['vk'].send_message.call_args.args[1]
        self.assertIn('[id11|Имя] 5.0 ⭐ (7 отзывов)',text)
        self.assertIn('Авто: Машина',text)
        self.assertIn('завершает текущую поездку',text)
        scope['kb'].passenger_ride_keyboard.assert_called_once_with(7)
        self.assertEqual(order.parallel_eta,5)

    def test_single_driver_card_has_inline_confirmation_v181(self):
        order,vk,_=previous.MessageChangesV173().eta()
        card=vk.send_tracked_message.call_args
        self.assertIn('⏰ Будет через 2 мин.',card.args[1])
        self.assertIn('Авто: Москвич 3, Серый, 893',card.args[1])
        self.assertIn('Ждёте машинку?',card.args[1])
        self.assertIn('keyboard',card.kwargs)
        self.assertFalse(any(c.args[0] == 22 for c in vk.send_message.call_args_list))

    def test_viewing_driver_list_restores_main_menu_for_current_ride(self):
        order=NS(id=7,status='assigned',arrival_eta=5,parallel_eta=None,driver_id=1)
        queue=NS(all_drivers=lambda s:[],actual_driver_statuses=lambda *a:{})
        scope={'queue_service':queue,'_direct_driver_candidates':lambda *a:[],
               '_active_orders_by_driver':lambda *a:{},'_render_passenger_driver_statuses':lambda items:[],
               'active_order_for':lambda *a:order,'get_state':lambda *a:NS(state='ride'),
               'States':NS(P_ADDR='enter'),'kb':Mock(),'vk':Mock()}
        load('bot/handlers.py','_show_driver_status_overview',scope)(Mock(),NS(vk_id=22))
        scope['kb'].passenger_ride_keyboard.assert_called_once_with(7)
        scope['kb'].passenger_waiting_keyboard.assert_not_called()

    def test_status_button_uses_ride_menu_for_parallel_driver_and_arrival_menu_on_site(self):
        for status,expected in (('parallel_assigned','passenger_ride_keyboard'),('arrived','passenger_arrived_keyboard')):
            with self.subTest(status=status):
                order=NS(id=7,number=1650,status=status,driver_id=None if status=='parallel_assigned' else 1,
                         parallel_driver_id=1 if status=='parallel_assigned' else None,
                         parallel_eta=5 if status=='parallel_assigned' else None,arrival_eta=5)
                scope={'passenger_order_for':lambda *a:order,'show_main_menu':Mock(),'kb':Mock(),'vk':Mock(),
                       'passenger_queue':Mock()}
                load('bot/handlers.py','passenger_order_status',scope)(Mock(),NS(vk_id=22),7)
                getattr(scope['kb'],expected).assert_called_once_with(7)
                scope['kb'].passenger_waiting_keyboard.assert_not_called()

    def test_real_card_rating_star_and_inflected_review_counts(self):
        scope={}
        for name in ('_passenger_review_word','driver_rating_text','departure_driver_card_text'):
            load('bot/order_service.py',name,scope)
        driver=NS(vk_id=141670738,full_name='Владислав Данилов',rating=5.0,rating_count=80,
                  car_model='Москвич 3',car_color='Серый',car_number='893')
        self.assertEqual(scope['departure_driver_card_text'](driver),
                         '[id141670738|Владислав Данилов] 5.0 ⭐ (80 отзывов)\nАвто: Москвич 3, Серый, 893')
        for count,word in ((1,'отзыв'),(2,'отзыва'),(11,'отзывов'),(21,'отзыв')):
            driver.rating_count=count
            self.assertIn(f'({count} {word})',scope['departure_driver_card_text'](driver))

    def test_no_reviews_have_no_fake_average(self):
        scope={}
        for name in ('_passenger_review_word','driver_rating_text','departure_driver_card_text'):
            load('bot/order_service.py',name,scope)
        driver=NS(vk_id=1,full_name=None,rating=5.0,rating_count=0,car_model=None,car_color=None,car_number=None)
        self.assertEqual(scope['departure_driver_card_text'](driver),'[id1|id1] — ⭐ (нет отзывов)\nАвто: не указано')

    def test_vk_keyboard_locations_and_bound_order(self):
        scope=keyboard_scope()
        main=json.loads(scope['passenger_ride_keyboard'](7))
        inline=json.loads(scope['passenger_departure_keyboard'](7))
        self.assertFalse(main.get('inline',False));self.assertTrue(inline['inline'])
        for keyboard in (main,inline):
            for row in keyboard['buttons']:
                for button in row:
                    self.assertEqual(json.loads(button['action']['payload'])['order_id'],7)
        self.assertEqual(len(main['buttons']),3);self.assertEqual(len(inline['buttons']),2)

    def response(self,cancel=False,text='Ждете?'):
        order=NS(id=7,status='assigned',driver_id=1,departure_prompt_outbox_id=99,actuality_confirmed=False)
        user=NS(vk_id=22);session=Mock();session.get.side_effect=[NS(vk_id=11),NS(text=text)]
        outbox=types.ModuleType('v173_runtime.outbox_service');outbox.cancel_or_delete=Mock();outbox.finalize_tracked_message=Mock()
        scope={'__package__':'v173_runtime','passenger_order_for':lambda *a:order,'show_main_menu':Mock(),
               'User':object,'OutboxMessage':object,'vk':Mock(),'kb':Mock(),'re':re,
               'passenger_cancel_request':Mock(),'passenger_cancel':Mock(),'set_state':Mock(),'States':NS(P_IN_RIDE='ride')}
        with relative_modules(outbox_service=outbox):
            load('bot/handlers.py','passenger_departure_response',scope)(session,user,cancel,7)
        return order,session,outbox,scope

    def test_yes_removes_only_question_no_ack_and_confirms(self):
        order,session,outbox,scope=self.response()
        outbox.cancel_or_delete.assert_called_once_with(session,99)
        scope['vk'].send_tracked_message.assert_called_once_with(22, 'Машинка в пути')
        scope['vk'].send_message.assert_called_once_with(11,'Клиент ожидает вас на месте')
        self.assertTrue(order.actuality_confirmed)
        self.assertIsNone(order.departure_prompt_outbox_id)
        scope['kb'].passenger_ride_keyboard.assert_not_called()

    def test_no_removes_buttons_then_uses_existing_cancel_flow(self):
        order,session,outbox,scope=self.response(cancel=True)
        outbox.cancel_or_delete.assert_not_called()
        self.assertEqual(outbox.finalize_tracked_message.call_args.args[2], '❌ Заявка отменена.')
        scope['passenger_cancel'].assert_called_once_with(session,NS(vk_id=22),7)
        scope['passenger_cancel_request'].assert_not_called()
        scope['vk'].send_message.assert_not_called()
        self.assertIsNone(order.departure_prompt_outbox_id)

    def test_legacy_full_card_is_preserved_but_inline_actions_are_removed(self):
        order,session,outbox,scope=self.response(text='Имя 5.0\nАвто: Машина\n⏰ Будет через 5 мин.\nЖдете?')
        outbox.cancel_or_delete.assert_not_called()
        call=outbox.finalize_tracked_message.call_args
        self.assertIn('Авто: Машина',call.args[2])
        self.assertNotIn('Ждете?',call.args[2])
        self.assertEqual(json.loads(call.kwargs['replacement_keyboard']),{'buttons':[],'inline':True})

    def test_main_card_is_protected_from_low_priority_backlog(self):
        scope=keyboard_scope()
        priority=load('bot/vk_client.py','_keyboard_priority',{'json':json})
        self.assertEqual(priority(scope['passenger_ride_keyboard'](7)),2000)
        self.assertEqual(priority(scope['passenger_departure_keyboard'](7)),0)
        self.assertEqual(load('bot/vk_client.py','_message_priority',{'re':re})('Ждете?'),2000)

class ArrivalAndCompatibilityV176(unittest.TestCase):
    def arrival(self,prompt=None,free=3,rate=10):
        order=NS(id=7,number=1650,passenger_id=2,driver_id=1,departure_prompt_outbox_id=99 if prompt else None)
        models={name:type(name,(),{}) for name in ('User','OutboxMessage')}
        session=Mock();session.get.side_effect=lambda model,id:NS(text=prompt) if model is models['OutboxMessage'] else NS(vk_id=11+id)
        outbox=types.ModuleType('v173_runtime.outbox_service');outbox.cancel_or_delete=Mock();outbox.finalize_tracked_message=Mock()
        scope={'__package__':'v173_runtime',**models,'dt':dt,'re':re,'timers':Mock(),'waiting_service':Mock(),
               'get_int':lambda *a:free,'get_float':lambda *a:rate,'_is_dispatcher_order':lambda o:False,
               'vk':Mock(),'kb':Mock(),'set_state':Mock(),'States':NS(P_ARRIVED='arrived')}
        with relative_modules(outbox_service=outbox):
            load('bot/order_service.py','start_free_waiting',scope)(session,order)
        return order,session,outbox,scope

    def test_arrival_exact_default_text_and_existing_waiting_flow(self):
        order,_,_,scope=self.arrival()
        self.assertEqual(scope['vk'].send_message.call_args.args[1],
                         '🚘 Водитель на месте! Машина: не указана\n⏰ У вас 3 мин бесплатного ожидания, далее 10 руб./мин.')
        self.assertEqual(order.status,'arrived')
        scope['waiting_service'].start_waiting.assert_called_once()
        scope['kb'].passenger_arrived_keyboard.assert_called_once_with(7)

    def test_arrival_text_uses_real_configured_tariff(self):
        *_,scope=self.arrival(free=5,rate=12.5)
        self.assertIn('5 мин бесплатного ожидания, далее 12.5 руб./мин.',scope['vk'].send_message.call_args.args[1])

    def test_arrival_deletes_new_question_only(self):
        order,session,outbox,_=self.arrival(prompt='Ждете?')
        outbox.cancel_or_delete.assert_called_once_with(session,99)
        outbox.finalize_tracked_message.assert_not_called()
        self.assertIsNone(order.departure_prompt_outbox_id)

    def test_arrival_preserves_legacy_driver_card(self):
        _,session,outbox,_=self.arrival(prompt='Имя\nАвто: Машина\nЖдете?')
        outbox.finalize_tracked_message.assert_called_once_with(session,99,'Имя\nАвто: Машина',replace_text=True,defer_edit=True)
        outbox.cancel_or_delete.assert_not_called()

    def test_pending_retired_refusal_notice_is_suppressed(self):
        row=NS(text='Все водители отказались от вашей заявки. Мы пробуем найти водителя через чат заявок.',status='sending',claimed_at=1)
        scope={'_order_action_ids':Mock(return_value=set())}
        self.assertTrue(load('bot/outbox_service.py','_cancel_if_order_is_stale',scope)(Mock(),row))
        self.assertEqual(row.status,'cancelled');scope['_order_action_ids'].assert_not_called()

    def test_forwarded_quote_of_refusal_is_not_suppressed(self):
        row=NS(text='Пассажир: Все водители отказались от вашей заявки.',status='sending')
        scope={'_order_action_ids':lambda row:set()}
        self.assertFalse(load('bot/outbox_service.py','_cancel_if_order_is_stale',scope)(Mock(),row))
        self.assertEqual(row.status,'sending')

    def test_delayed_waiting_menu_cannot_overwrite_active_ride_menu(self):
        row=NS(id=10,text='⏰ Чуть подождать...',keyboard='{"buttons":[],"inline":false}',status='sending',claimed_at=1)
        # Build the actual serialized VK persistent keyboard.
        scope=keyboard_scope()
        load('bot/keyboards.py','passenger_waiting_keyboard',scope)
        row.keyboard=scope['passenger_waiting_keyboard'](7)
        extraction={'json':json,'re':re,'_ORDER_ACTION_COMMANDS':{'order_status'}}
        action_ids=load('bot/outbox_service.py','_order_action_ids',extraction)
        runtime={'_order_action_ids':action_ids,'Order':object,'log':Mock()}
        session=Mock();session.get.return_value=NS(status='assigned')
        self.assertTrue(load('bot/outbox_service.py','_cancel_if_order_is_stale',runtime)(session,row))
        self.assertEqual(row.status,'cancelled')

    def test_current_waiting_menu_remains_deliverable(self):
        row=NS(id=10,text='⏰ Минутку…',keyboard='{"buttons":[[{"action":{"payload":"order_status"}}]]}',status='sending')
        session=Mock();session.get.return_value=NS(status='searching')
        runtime={'_order_action_ids':lambda row:{7},'Order':object,'log':Mock()}
        self.assertFalse(load('bot/outbox_service.py','_cancel_if_order_is_stale',runtime)(session,row))
        self.assertEqual(row.status,'sending')

    def test_serialized_driver_main_menu_is_also_protected(self):
        scope=keyboard_scope();load('bot/keyboards.py','driver_delivery_keyboard',scope)
        # Delivery menu finish controls are persistent and must be recognized
        # even though VK JSON-escapes the nested action payload.
        menu=json.dumps({'buttons':[[{'action':{'payload':json.dumps({'cmd':'finish'})}}]],'inline':False})
        priority=load('bot/vk_client.py','_keyboard_priority',{'json':json})
        self.assertEqual(priority(menu),2000)
        self.assertEqual(priority('{invalid'),0)
        self.assertEqual(priority(json.dumps({'buttons':[[{'action':{'payload':'{invalid'}}]]})),0)

    def override(self,default,template,function='dispatch_new_order'):
        scope={'callsite_key':lambda *a:'key','DEFAULTS':{'key':('title','factory')},
               '_cached_rows':lambda:{'key':(template,None)},'_default_text':lambda key:'factory'}
        return load('common/bot_messages_service.py','render_outgoing',scope)(default,'file',function,1,{})

    def test_old_refusal_template_cannot_return_removed_message(self):
        self.assertEqual(self.override('Заявка отправлена в чат заявок.',
                         'Все водители отказались от вашей заявки. Мы пробуем найти водителя'),
                         'Заявка отправлена в чат заявок.')

    def test_wrong_waiting_template_cannot_replace_free_driver_outcome(self):
        self.assertEqual(self.override('⏰ Минутку…','⏰ Чуть подождать, первый свободный, возьмет вашу заявку…'),'⏰ Минутку…')

    def test_old_eta_only_template_cannot_strip_car_and_identity(self):
        card='[id1|Имя] 5.0 ⭐ (7 отзывов)\nАвто: Машина\n⏱ Доставит за 5 мин.'
        self.assertEqual(self.override(card,'⏱ Водитель выполнит доставку примерно за 5 мин.','_apply_eta'),card)

    def test_old_arrival_instructions_cannot_replace_new_tariff_text(self):
        text='Водитель на месте! У вас 3 мин бесплатного ожидания, далее 10 руб./мин.'
        self.assertEqual(self.override(text,'Водитель на месте! Выберите «Выхожу» или «Подождать».','start_free_waiting'),text)

    def test_valid_custom_templates_remain_available(self):
        self.assertEqual(self.override('⏰ Минутку…','Передаём заявку водителю.'),'Передаём заявку водителю.')
        self.assertEqual(self.override('Имя\nАвто: Машина','{default}','_apply_eta'),'Имя\nАвто: Машина')

if __name__=='__main__':unittest.main()
