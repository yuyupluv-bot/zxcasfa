"""V174: no fictional route notices; exclusive selection-to-offer handoff."""
from __future__ import annotations
import datetime as dt
import json
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load, relative_modules, source
import types

class TemplatesV174(unittest.TestCase):
    def render(self, template, default, context=None):
        key='auto.test'
        scope={'callsite_key':lambda *a:key, 'DEFAULTS':{key:('Test','{default}')},
               '_cached_rows':lambda:{key:(template,None)}, '_default_text':lambda k:'{default}'}
        return load('common/bot_messages_service.py','render_outgoing',scope)(default,'f.py','f',1,context)

    def test_old_route_notice_cannot_replace_actual_waiting_or_free_status(self):
        template='У нас есть водитель, который поехал до {city_form}. Мы его уведомим.'
        for default in ('⏰ Минутку…','⏰ Чуть подождать, первый свободный, возьмет вашу заявку…'):
            self.assertEqual(self.render(template,default,{'city_form':'Пашии'}),default)
            self.assertEqual(self.render(template,default),default)

    def test_missing_template_variable_never_leaks_to_passenger(self):
        self.assertEqual(self.render('Цена {missing}', 'Нормальный текст'), 'Нормальный текст')

    def test_old_unavailable_notice_cannot_replace_success(self):
        stale='Выбранный водитель уже недоступен. Заявка не передавалась в общую очередь.'
        default='✅ Заявка #1067 создана и отправлена водителю.'
        self.assertEqual(self.render(stale,default),default)

    def test_complete_custom_template_still_works(self):
        self.assertEqual(self.render('Маршрут: {route}. {default}', 'Ожидайте.', {'route':'Пашия → город'}),
                         'Маршрут: Пашия → город. Ожидайте.')
        default='У нас есть водитель, который поехал до Пашии.'
        self.assertEqual(self.render('У нас есть водитель, который поехал до {city_form}.',default,{'city_form':'Пашии'}),default)

class VillageRouteV174(unittest.TestCase):
    def scope(self):
        scope={'re':re,'json':json,'ACTIVE':('assigned','arrived','in_progress')}
        # Read pattern constants from the actual code through AST.
        import ast
        t=ast.parse(source('bot/parallel_orders.py'))
        assignment=next(n for n in t.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='ROUTE_CITY_PATTERNS' for x in n.targets))
        exec(compile(ast.Module(body=[assignment],type_ignores=[]),'<patterns>','exec'),scope)
        for name in ('_route_city_in','_route_dash_parts','_destination_city','_origin_city',
                     '_city_first_two','_city_before_to_separator','_has_return_intent','route_priority_city'):
            load('bot/parallel_orders.py',name,scope)
        return scope

    def test_pashiya_pickup_is_not_a_trip_to_pashiya(self):
        scope=self.scope(); scope['Order']=NS(status=Mock(),driver_id=Mock(),driver_departed_at=Mock())
        session=Mock()
        session.query.return_value.filter.return_value.all.return_value=[NS(route_text='Пашия ул Ленина 10 до города',address_to='до города')]
        candidates=load('bot/parallel_orders.py','_eligible_departed_orders_to_city',scope)(session,'Пашия')
        self.assertEqual(candidates,[])

    def test_trip_to_pashiya_is_detected_but_return_trip_is_not(self):
        scope=self.scope(); scope['Order']=NS(status=Mock(),driver_id=Mock(),driver_departed_at=Mock())
        genuine=NS(route_text='Ленина 10 до Пашии',address_to='Пашия')
        returning=NS(route_text='Ленина 10 до Пашии с обратом',address_to='Пашия')
        session=Mock();session.query.return_value.filter.return_value.all.return_value=[genuine,returning]
        self.assertEqual(load('bot/parallel_orders.py','_eligible_departed_orders_to_city',scope)(session,'Пашия'),[genuine])

    def test_stale_route_offer_is_not_proof_of_existing_driver(self):
        order=NS(id=7,status='queued',parallel_offer_driver_id=1,parallel_offer_trip_id=10,
                 parallel_auto_excluded_driver_ids=None)
        session=Mock();session.get.return_value=None
        scope={'Order':object,'PARALLEL_CANDIDATE_STATUSES':('queued',),
               'route_priority_city':lambda o:'Пашия','_remove_notifications':Mock(),
               '_free_driver_has_priority':lambda *a:False,'refresh_busy_driver_menus':Mock(),
               'is_route_fallback':lambda o:False,'_claim_auto_offer_trip':Mock(return_value=None)}
        result=load('bot/parallel_orders.py','notify_busy_drivers',scope)(session,order)
        self.assertFalse(result)
        scope['_remove_notifications'].assert_called_once_with(session,order)

    def test_no_pashiya_or_route_driver_goes_to_real_gorno_fallback(self):
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.route_priority_city=lambda o:'Пашия';parallel.free_line_city=lambda o:'Пашия'
        parallel.notify_busy_drivers=Mock(return_value=False);parallel._fallback_to_free_drivers=Mock()
        session=Mock();session.get.return_value=NS(vk_id=22)
        order=NS(id=7,passenger_id=2,dispatcher_id=None,status='queued',offered_driver_id=None,driver_id=None)
        scope={'__package__':'v173_runtime','User':object,'queue_service':NS(has_waiting_driver=lambda *a,**kw:False),
               'enqueue':Mock(),'vk':Mock(),'kb':Mock()}
        with relative_modules(parallel_orders=parallel):
            load('bot/passenger_queue.py','dispatch_new_order',scope)(session,order)
        scope['vk'].send_message.assert_called_once()
        self.assertEqual(scope['vk'].send_message.call_args.args[1],
                         '⏰ Чуть подождать, первый свободный, возьмет вашу заявку…')
        parallel._fallback_to_free_drivers.assert_called_once_with(session,order,recheck_village=True)

class PersonalOfferV174(unittest.TestCase):
    def run_offer(self, queue_status='offered', expired=False, busy=False, blocked=False, enqueue=True):
        order=NS(id=7,number=1067,passenger_id=2,requested_driver_id=1,driver_id=None,parallel_driver_id=None,
                 status='created',customer_price=None,actuality_confirmed=False,comment=None,night_surcharge=False,
                 voice_attachment=None,dispatcher_id=None,offered_driver_id=None)
        driver=NS(id=1,vk_id=11,is_on_line=True,driver_status='online',is_blocked=blocked)
        entry=NS(status=queue_status)
        selection=NS(expires_at=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=-10 if expired else 80))
        models={name:type(name,(),{'id':0,'driver_id':0,'passenger_id':0}) for name in ('Order','DriverQueue','DriverSelection','User')}
        session=Mock(); queries={}
        for name,row in [('Order',order),('DriverQueue',entry),('DriverSelection',selection)]:
            q=Mock();q.filter.return_value.with_for_update.return_value.one_or_none.return_value=row;queries[models[name]]=q
        session.query.side_effect=lambda model:queries[model]
        session.get.return_value=NS(vk_id=22,full_name='Пассажир')
        pq=types.ModuleType('v173_runtime.passenger_queue');pq.pause_actuality_prompts=Mock()
        push=types.ModuleType('v173_runtime.push_service');push.send=Mock()
        vk=Mock();vk.prepare_voice_attachment.return_value=None;vk.send_tracked_message.return_value=123 if enqueue else None
        scope={**models,'__package__':'v173_runtime','dt':dt,'log':Mock(),'vk':vk,
               'queue_service':NS(_driver_has_active_work=lambda *a:busy),
               '_set_current_offer':lambda s,o,d:setattr(o,'offered_driver_id',d),
               'order_type_label':lambda o:'Обычная','order_text':lambda o:'Ленина 10 до города',
               'passenger_rating_text':lambda u:'5.0','_extras_summary':lambda *a:'',
               'is_delivery_type':lambda o:False,'kb':Mock(),'set_state':Mock(),
               'States':NS(D_OFFER='offer'),'get_int':lambda *a:60,'timers':Mock(), '_accept_timeout':Mock()}
        parallel=types.ModuleType('v173_runtime.parallel_orders')
        parallel.local_line_allows=lambda order,driver: True # this fixture is non-local
        with relative_modules(passenger_queue=pq,push_service=push,parallel_orders=parallel):
            result=load('bot/order_service.py','offer_to_selected_driver',scope)(session,order,driver)
        return result,order,entry,session,vk,scope

    def test_held_driver_receives_personal_order(self):
        result,order,entry,session,vk,scope=self.run_offer()
        self.assertTrue(result);self.assertEqual(order.offered_driver_id,1)
        self.assertEqual(order.status,'searching');self.assertEqual(entry.status,'offered')
        vk.send_tracked_message.assert_called_once();self.assertEqual(order.offer_outbox_id,123)
        self.assertIn('Пассажир выбрал вас.',vk.send_tracked_message.call_args.args[1])

    def test_valid_hold_repairs_waiting_queue_without_fifo(self):
        result,order,entry,session,vk,scope=self.run_offer(queue_status='waiting')
        self.assertTrue(result);self.assertEqual(entry.status,'offered')
        session.flush.assert_called()
        self.assertNotIn('next_waiting_driver',source('bot/order_service.py').split('def offer_to_selected_driver',1)[1].split('def offer_to_next_driver',1)[0])

    def test_busy_expired_blocked_or_away_driver_is_not_claimed(self):
        for kwargs in ({'busy':True},{'expired':True},{'blocked':True},{'queue_status':'away'}):
            with self.subTest(kwargs=kwargs):
                result,_,_,_,vk,_=self.run_offer(**kwargs)
                self.assertFalse(result);vk.send_tracked_message.assert_not_called()

    def test_failed_outbox_is_not_reported_as_success(self):
        result,order,_,_,_,_=self.run_offer(enqueue=False)
        self.assertFalse(result);self.assertIsNone(order.offered_driver_id)

    def test_hold_is_consumed_only_after_real_offer_and_confirmation(self):
        import ast
        text=source('bot/handlers.py')
        create=ast.get_source_segment(text,next(n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef) and n.name=='create_passenger_order'))
        handoff=create.index('offered = order_service.offer_to_selected_driver')
        deletion=create.index('session.delete(direct_selection)')
        confirmation=create.index('создана и отправлена водителю.')
        self.assertLess(handoff,deletion);self.assertLess(deletion,confirmation)
        self.assertIn('if not queue_service._driver_has_active_work(session, selected_driver.id):',create)

    def test_creation_holds_reservation_until_offer_then_sends_success(self):
        selection=NS(id=9,driver_id=1,passenger_id=2,
                     expires_at=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=80))
        driver=NS(id=1,vk_id=11,is_on_line=True,driver_status='online')
        user=NS(id=2,vk_id=22)
        entry=NS(status='waiting')
        events=[];deleted=[];created=[]
        class Order:
            def __init__(self,**kwargs):
                self.__dict__.update(kwargs);self.id=7;self.number=1067;self.offered_driver_id=None
        DS=type('DriverSelection',(),{'id':0,'passenger_id':0,'driver_id':0})
        DQ=type('DriverQueue',(),{'driver_id':0})
        class Session:
            def query(self,model):
                q=Mock()
                q.filter.return_value.with_for_update.return_value.one_or_none.return_value=selection if model is DS else entry
                return q
            def get(self,*args): return driver
            def add(self,row): created.append(row)
            def flush(self): events.append('flush')
            def delete(self,row): deleted.append(row);events.append('delete hold')
        session=Session()
        def offer(s,o,d):
            self.assertEqual(deleted,[])
            self.assertEqual(o.requested_driver_id,driver.id)
            self.assertEqual(d.id,driver.id)
            events.append('offer')
            entry.status='offered';o.offered_driver_id=d.id;o.status='searching'
            return True
        service=NS(offer_to_selected_driver=offer)
        scope={'dt':dt,'Order':Order,'DriverSelection':DS,'DriverQueue':DQ,'User':object,
               'get_data':lambda *a:{'draft':{'requested_driver_id':1,'driver_selection_id':9,
                      'address_from':'Ленина 10','address_to':'Мира 7','route_text':'Ленина 10 до Мира 7'}},
               'active_order_for':lambda *a,**kw:None,'offered_order_for':lambda *a:None,
               '_recent_passenger_order_count':lambda *a:0,'_notice_existing_direct_order':Mock(),
               'time_utils':NS(now=lambda:dt.datetime.now(dt.timezone.utc)),
               'delivery_service':NS(PARTNER_DELIVERY_TYPE='partner_delivery'),
               'extra_services':NS(to_json=lambda a:'[]'),'night_tariff':NS(is_night=lambda *a:False),
               'audit':Mock(),'timers':Mock(),'set_state':Mock(),'States':NS(P_WAITING='waiting'),
               'order_service':service,'kb':Mock(),
               'vk':NS(send_message=lambda *a,**kw:events.append(('message',a[1])))}
        load('bot/handlers.py','create_passenger_order',scope)(session,user,'regular')
        self.assertEqual(deleted,[selection])
        self.assertEqual(created[0].status,'searching')
        self.assertLess(events.index('offer'),events.index('delete hold'))
        messages=[e for e in events if isinstance(e,tuple)]
        self.assertEqual(messages,[('message','✅ Заявка #1067 создана и отправлена водителю.')])
        self.assertGreater(events.index(messages[0]),events.index('offer'))
