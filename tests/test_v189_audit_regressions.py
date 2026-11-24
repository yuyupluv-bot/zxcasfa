"""V189 regression checks (all must pass). Original audit reproductions. No expectedFailure markers are used.
Run with original project at /data/project/zxcasfa-main. No production code modified.
Network, ORM boundaries and clock are mocked; original functions are executed.
"""
import sys,datetime as dt,threading,unittest,types
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from contextlib import contextmanager
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_requirements_v173_requested_changes import load,relative_modules
NOW=dt.datetime(2026,11,24,15,0,tzinfo=dt.timezone.utc)
class Field:
 def __eq__(self,v):return self
 def __ne__(self,v):return self
 def __or__(self,v):return self
 def in_(self,v):return self
 def isnot(self,v):return self
 def asc(self):return self
F=Field()
Order=NS(id=F,status=F,driver_id=F,parallel_driver_id=F,parallel_offer_driver_id=F)
User=NS(id=F,parallel_notice_outbox_id=F,parallel_notice_count=F)

def handoff_failure():
 order=NS(id=7,number=7,status='searching',offered_driver_id=1,requested_driver_id=None,
  passenger_id=2,declined_driver_ids='[]',decline_count=0,decline_reasons_json='[]',
  last_decline_reason=None,voice_attachment=None,offer_outbox_id=77,
  eta_offer_expires_at=NOW+dt.timedelta(seconds=40))
 user=NS(id=1,vk_id=101)
 session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
 service=Mock();service._current_offer.return_value=1
 service.publish_special_decline_to_requests_chat.return_value=False
 scope=dict(Order=Order,order_service=service,queue_service=Mock(),timers=Mock(),
  can_switch_role=lambda u:False,kb=Mock(),States=NS(D_OFFER='driver_offer'),
  set_state=Mock(),vk=Mock(),get_int=lambda *a:30,time_utils=NS(now=lambda:NOW),dt=dt,customer_price_service=NS(has_price=lambda o:False))
 parallel=types.ModuleType('v173_runtime.parallel_orders');parallel.local_village_line=lambda o:None
 scope['__package__']='v173_runtime'
 with relative_modules(parallel_orders=parallel):
  load('bot/handlers.py','driver_decline',scope)(session,user,7,'to_driver_chat')
 return order,scope

class AuditReproductions(unittest.TestCase):
 def test_handoff_recovery_timer_matches_durable_deadline(self):
  order,scope=handoff_failure()
  delay=scope['timers'].schedule.call_args.args[2]
  remaining=(order.eta_offer_expires_at-NOW).total_seconds()
  self.assertEqual(delay,remaining,'Restored timer is 30s, but acceptance deadline still has 40s')

 def test_early_timeout_keeps_recovery_job(self):
  # The above handoff schedules +30s while the old offer expires at +40s.
  order,unused=handoff_failure()
  session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
  @contextmanager
  def sessions():yield session
  database=types.ModuleType('common.database');database.session_scope=sessions
  class Clock(dt.datetime):
   @classmethod
   def now(cls,tz=None):return NOW+dt.timedelta(seconds=30)
  scope=dict(Order=Order,dt=NS(datetime=Clock,timezone=dt.timezone),log=Mock(),
   _current_offer=lambda *a:1,timers=Mock())
  callback=load('bot/order_service.py','_accept_timeout',scope)
  delete=Mock()
  safe=dict(log=Mock(),_condition=threading.Condition(),_entries={},
   _key=lambda k,i:f'{k}:{i}',_delete_persistent=delete)
  scope['timers'].schedule.side_effect=lambda kind,oid,*args:safe['_entries'].update({f'{kind}:{oid}':args})
  with patch.dict(sys.modules,{'common.database':database}):
   load('bot/timers.py','_safe_run',safe)('accept',7,lambda:callback(7,1))
  self.assertEqual(order.status,'searching')
  delete.assert_not_called()  # Actual: accept:7 is deleted, no remaining watchdog source.

 def test_parallel_counter_excludes_blacklisted_only_customer(self):
  candidate=NS(id=7,passenger_id=2)
  current=NS(driver_id=1,route_text='Ленина 1 — Мира 2',address_from='Ленина 1')
  driver=NS(id=1,parallel_notice_outbox_id=None,parallel_notice_count=0)
  session=Mock();q1=Mock();q2=Mock()
  q1.filter.return_value.all.return_value=[current]
  q2.filter.return_value.with_for_update.return_value.all.return_value=[driver]
  session.query.side_effect=[q1,q2];session.get.return_value=driver
  sync=Mock()
  scope=dict(Order=Order,User=User,ACTIVE=('assigned','arrived','in_progress'),
   _remove_legacy_gorno_aggregate_refs=Mock(),available=lambda s:[candidate],
   route_priority_city=lambda o:None,_origin_city=lambda r:None,
   _city_before_to_separator=lambda r:None,_city_first_two=lambda r:None,
   _is_gornozavodsk_trip=lambda o:True,_sync_parallel_notice=sync,
   local_line_allows=lambda *a:True,_destination_restricted_orders=lambda c,o:o,
   _russian_request_word=lambda n:'заявка',
   blacklist_service=NS(blocks=lambda s,d,p:True))
  load('bot/parallel_orders.py','refresh_gornozavodsk_parallel_notices',scope)(session)
  self.assertEqual(sync.call_args.args[2],0,'Only candidate is blacklisted, but driver receives count=1')

 def delivery_accept(self):
  order=NS(id=7,number=7,status='searching',passenger_id=2,driver_id=None,
   offered_driver_id=1,dispatcher_id=None,customer_price=None,chat_notice_outbox_id=None,
   eta_offer_source='fifo',created_at=NOW-dt.timedelta(seconds=30),
   driver_accept_time=None,arrival_eta=None,offer_outbox_id=77,price=300)
  driver=NS(id=1,vk_id=101,is_on_line=True,current_line='Горнозаводск',driver_missed_offers=0)
  session=Mock();session.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
  scope=dict(Order=Order,order_service=Mock(),parallel_orders=NS(local_line_allows=lambda *a:True),
   blacklist_service=NS(blocks=lambda *a:False),offer_deadline_passed=lambda *a:False,
   driver_block_service=NS(is_blocked=lambda u:False),lines=NS(has_complete_car=lambda u:True),
   timers=Mock(),time_utils=NS(now=lambda:NOW),queue_service=Mock(),audit=Mock(),
   request_claim_actuality=Mock(return_value=False),claim_needs_actuality=lambda o:False,
   delivery_service=Mock(),_apply_eta=Mock(),vk=Mock())
  scope['order_service']._current_offer.return_value=1
  scope['delivery_service'].is_delivery.return_value=True
  load('bot/handlers.py','driver_accept',scope)(session,driver,7,notify_passenger=False,selected_eta=10)
  return session,order,driver,scope

 def test_fresh_delivery_retires_original_eta_offer(self):
  session,order,driver,scope=self.delivery_accept()
  scope['delivery_service'].request_price.assert_called_once()
  scope['order_service'].finalize_offer_message.assert_called_once_with(session,order,'⏰ Вы выбрали 10 мин.')

 def test_delivery_confirmation_does_not_orphan_old_offer_pointer(self):
  session,order,driver,scope=self.delivery_accept()
  vk=Mock();vk.send_tracked_message.return_value=88
  other=dict(_now=lambda:NOW,vk=vk,kb=Mock(),set_state=Mock(),States=NS(D_ETA_MENU='eta'),audit=Mock(),order_service=scope['order_service'])
  load('bot/delivery_service.py','_confirm',other)(session,order,None,driver)
  self.assertEqual(order.offer_outbox_id,88)
  # No call retired 77 before it was overwritten. The original audit assertion now passes.
  scope['order_service'].finalize_offer_message.assert_called()

 def test_parallel_claim_is_not_accepted_before_eta_selection(self):
  driver=NS(id=1,vk_id=101)
  current=NS(id=6,driver_id=1)
  order=NS(id=7,number=7,passenger_id=2,status='queued',customer_price=None,
   driver_id=None,parallel_driver_id=None,parallel_accepted_at=None,dispatcher_id=None,
   route_text='Мира 2',voice_attachment=None,parallel_offer_driver_id=None,parallel_offer_trip_id=None)
  passenger=NS(vk_id=202,full_name='Клиент')
  session=Mock();userq=Mock();currentq=Mock();existingq=Mock();candidateq=Mock();deleteq=Mock()
  userq.filter.return_value.with_for_update.return_value.first.return_value=driver
  currentq.filter.return_value.with_for_update.return_value.first.return_value=current
  existingq.filter.return_value.first.return_value=None
  candidateq.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
  otherq=Mock();otherq.filter.return_value.first.return_value=None
  session.query.side_effect=[userq,currentq,existingq,candidateq,otherq]
  session.get.return_value=passenger
  helpers=types.ModuleType('v173_runtime.handlers');helpers.request_claim_actuality=lambda *a,**k:False
  push=types.ModuleType('v173_runtime.push_service');push.send=Mock()
  scope=dict(__package__='v173_runtime',User=User,Order=Order,PassengerQueue=NS(order_id=F),
   ACTIVE=('assigned','arrived','in_progress'),blacklist_service=NS(blocks=lambda *a:False),
   _parallel_candidate_filter=lambda q:q,local_line_allows=lambda *a:True,
   _free_driver_has_priority=lambda *a:False,_destination_restricted_orders=lambda c,o:o,
   timers=Mock(),time_utils=NS(now=lambda:NOW),queue_service=Mock(),_remove_notifications=Mock(),
   refresh_busy_driver_menus=Mock(),vk=Mock(),kb=Mock(),_reserve_eta_choice=Mock(return_value=order))
  with relative_modules(handlers=helpers,push_service=push):
   load('bot/parallel_orders.py','take',scope)(session,driver,7)
  self.assertIsNone(order.parallel_driver_id,'Assigned immediately, then given 120 seconds to choose ETA')




class ParallelChoiceRuntime(unittest.TestCase):
 def reserve(self,repeat=False,expired=False):
  order=NS(id=7,status='queued',parallel_driver_id=None,parallel_accepted_at=None,
   parallel_offer_driver_id=1 if repeat else None,
   eta_offer_source='parallel' if repeat else None,
   eta_offer_expires_at=NOW+dt.timedelta(seconds=-1 if expired else 12) if repeat else None,
   voice_attachment=None)
  driver=NS(id=1,vk_id=101,is_on_line=True,driver_status='busy')
  current=NS(id=6,status='in_progress',driver_id=1)
  passenger_queue=types.ModuleType('v173_runtime.passenger_queue');passenger_queue.remove=Mock()
  scope=dict(__package__='v173_runtime',time_utils=NS(now=lambda:NOW),dt=dt,json=__import__('json'),
   vk=Mock(),kb=Mock(),timers=Mock(),_route_offer_card_text=lambda *a:'request')
  scope['vk'].send_tracked_message.return_value=77
  load('bot/parallel_orders.py','_eta_offer_remaining',scope)
  with relative_modules(passenger_queue=passenger_queue):
   result=load('bot/parallel_orders.py','_reserve_eta_choice',scope)(Mock(),driver,current,order)
  return order,driver,current,scope,passenger_queue

 def test_reservation_does_not_accept_or_change_current_ride(self):
  order,driver,current,scope,pq=self.reserve()
  self.assertEqual(order.status,'queued');self.assertIsNone(order.parallel_driver_id)
  self.assertIsNone(order.parallel_accepted_at);self.assertEqual(current.status,'in_progress')
  self.assertEqual(driver.driver_status,'busy');self.assertTrue(driver.is_on_line)
  self.assertEqual(order.parallel_offer_driver_id,1)
  self.assertEqual(order.eta_offer_expires_at,NOW+dt.timedelta(seconds=60))
  self.assertEqual(scope['timers'].schedule.call_args.args[:3],('route_parallel_offer',7,60))
  pq.remove.assert_called_once()

 def test_duplicate_choice_does_not_extend_deadline_or_resend_card(self):
  order,_,_,scope,pq=self.reserve(repeat=True)
  self.assertEqual(order.eta_offer_expires_at,NOW+dt.timedelta(seconds=12))
  scope['timers'].schedule.assert_not_called();scope['vk'].send_tracked_message.assert_not_called()
  pq.remove.assert_not_called()

 def test_expired_reopen_does_not_extend_deadline(self):
  order,_,_,scope,pq=self.reserve(repeat=True,expired=True)
  self.assertLess(order.eta_offer_expires_at,NOW)
  scope['timers'].schedule.assert_not_called();self.assertIsNone(order.parallel_driver_id)

 def accepted(self,owner=1,remaining=30,blocked=False,trip=6,wait=False,eta=10):
  driver=NS(id=1,vk_id=101,is_on_line=True,driver_status='busy')
  current=NS(id=6,status='in_progress',driver_id=1)
  order=NS(id=7,number=7,status='queued',passenger_id=2,driver_id=None,parallel_driver_id=None,
   parallel_offer_driver_id=owner,parallel_offer_trip_id=trip,eta_offer_source='parallel',
   eta_offer_expires_at=NOW+dt.timedelta(seconds=remaining),customer_price=None,agreed_price=None,
   chat_notice_outbox_id=None,dispatcher_id=None,parallel_eta=None)
  passenger=NS(vk_id=202,full_name='Client')
  session=Mock();a=Mock();b=Mock();c=Mock();d=Mock();e=Mock()
  a.filter.return_value.with_for_update.return_value.first.return_value=driver
  b.filter.return_value.with_for_update.return_value.first.return_value=current
  c.filter.return_value.first.return_value=None
  d.filter.return_value.with_for_update.return_value.one_or_none.return_value=order
  session.query.side_effect=[a,b,c,d,e];session.get.return_value=passenger
  handlers=types.ModuleType('v173_runtime.handlers');handlers.request_claim_actuality=Mock(return_value=wait)
  scope=dict(__package__='v173_runtime',Order=Order,User=User,PassengerQueue=NS(order_id=F),
   ACTIVE=('assigned','arrived','in_progress'),PARALLEL_CANDIDATE_STATUSES=('created','queued','no_drivers','chat_search'),
   dt=dt,time_utils=NS(now=lambda:NOW),_parallel_candidate_filter=lambda q:q,
   blacklist_service=NS(blocks=lambda *a:blocked),local_line_allows=lambda *a:True,
   _free_driver_has_priority=lambda *a:True,_destination_restricted_orders=lambda c,o:o,
   timers=Mock(),queue_service=Mock(),_remove_notifications=Mock(),refresh_busy_driver_menus=Mock(),
   _finalize_route_offer_card=Mock(),vk=Mock(),kb=Mock(),save_eta=Mock())
  load('bot/parallel_orders.py','_eta_offer_remaining',scope)
  with relative_modules(handlers=handlers):
   load('bot/parallel_orders.py','take',scope)(session,driver,7,selected_eta=eta)
  return order,current,driver,scope,handlers

 def test_only_eta_assigns_parallel_ride_and_stores_acceptance_time(self):
  order,current,driver,scope,helpers=self.accepted()
  self.assertEqual(order.status,'parallel_assigned');self.assertEqual(order.parallel_driver_id,1)
  self.assertEqual(order.parallel_accepted_at,NOW);self.assertEqual(order.parallel_eta,10)
  self.assertIsNone(order.driver_id);self.assertEqual(current.status,'in_progress')
  self.assertTrue(driver.is_on_line);self.assertEqual(driver.driver_status,'busy')
  scope['save_eta'].assert_called_once();scope['timers'].schedule.assert_not_called()

 def test_exact_deadline_cannot_accept(self):
  order,_,_,scope,_=self.accepted(remaining=0)
  self.assertIsNone(order.parallel_driver_id);scope['queue_service'].mark_assigned.assert_not_called()

 def test_foreign_reservation_cannot_accept(self):
  order,_,_,scope,_=self.accepted(owner=3)
  self.assertIsNone(order.parallel_driver_id);scope['save_eta'].assert_not_called()

 def test_new_blacklist_entry_cannot_be_bypassed_by_old_eta_button(self):
  order,_,_,scope,_=self.accepted(blocked=True)
  self.assertIsNone(order.parallel_driver_id);scope['save_eta'].assert_not_called()

 def test_changed_current_ride_cannot_accept_old_offer(self):
  order,_,_,scope,_=self.accepted(trip=9)
  self.assertIsNone(order.parallel_driver_id);scope['save_eta'].assert_not_called()

 def test_old_request_keeps_eta_while_waiting_for_passenger(self):
  order,current,driver,scope,helpers=self.accepted(wait=True)
  self.assertEqual(order.parallel_eta,10);self.assertEqual(current.status,'in_progress')
  scope['save_eta'].assert_not_called();scope['timers'].schedule.assert_not_called()
  helpers.request_claim_actuality.assert_called_once()

 def test_confirmation_uses_stored_eta_without_a_second_timer(self):
  order=NS(id=7,status='parallel_assigned',parallel_driver_id=1,parallel_eta=10)
  scope=dict(vk=Mock(),save_eta=Mock(),timers=Mock())
  load('bot/parallel_orders.py','prompt_eta_after_actuality',scope)(Mock(),NS(id=1),order)
  scope['save_eta'].assert_called_once();scope['timers'].schedule.assert_not_called()

 def expire(self,timed_out=True):
  driver=NS(id=1,vk_id=101,is_on_line=True,driver_status='busy')
  current=NS(id=6,status='in_progress',driver_id=1)
  order=NS(id=7,status='queued',parallel_offer_driver_id=1,parallel_driver_id=None)
  session=Mock();session.get.return_value=driver
  session.query.return_value.filter.return_value.first.return_value=current
  service=types.ModuleType('v173_runtime.order_service');service.offer_to_next_driver=Mock()
  pq=types.ModuleType('v173_runtime.passenger_queue');pq.enqueue=Mock()
  queue=Mock()
  def leave(s,d):d.is_on_line=False;d.driver_status='offline'
  queue.leave_queue.side_effect=leave
  scope=dict(__package__='v173_runtime',User=User,Order=Order,ACTIVE=('assigned','arrived','in_progress'),
   queue_service=queue,vk=Mock(),refresh_busy_driver_menus=Mock(),_finalize_route_offer_card=Mock(),_exclude_from_auto_offer=Mock())
  with relative_modules(order_service=service,passenger_queue=pq):
   load('bot/parallel_orders.py','_expire_eta_choice',scope)(session,order,timed_out=timed_out)
  return order,current,driver,scope,service

 def test_timeout_removes_availability_but_not_current_trip(self):
  order,current,driver,scope,service=self.expire()
  self.assertFalse(driver.is_on_line);self.assertEqual(driver.driver_status,'busy')
  self.assertEqual(current.status,'in_progress');self.assertEqual(current.driver_id,1)
  self.assertEqual(order.status,'queued');self.assertIsNone(order.parallel_driver_id)
  self.assertEqual(service.offer_to_next_driver.call_args.args[2],{1})
  scope['_exclude_from_auto_offer'].assert_called_once_with(order,1)
  self.assertTrue(current.parallel_auto_offers_disabled)
  self.assertNotIn('set_state',scope)  # Fare/chat input is never reset by this helper.

 def test_timely_decline_keeps_line_and_current_trip(self):
  order,current,driver,scope,service=self.expire(timed_out=False)
  self.assertTrue(driver.is_on_line);self.assertEqual(current.status,'in_progress')
  scope['queue_service'].leave_queue.assert_not_called()

 def test_parallel_age_uses_new_acceptance_not_previous_driver_timestamp(self):
  order=NS(created_at=NOW-dt.timedelta(minutes=10),driver_accept_time=NOW-dt.timedelta(minutes=9),parallel_accepted_at=NOW)
  needs=load('bot/handlers.py','claim_needs_actuality',dict(time_utils=NS(now=lambda:NOW),dt=dt))
  self.assertTrue(needs(order,parallel=True));self.assertFalse(needs(order))

 def test_zero_and_negative_eta_do_not_clamp_to_acceptance(self):
  for eta in (0,-1,1441,'bad',None):
   scope=dict(vk=Mock());session=Mock()
   load('bot/parallel_orders.py','save_eta',scope)(session,NS(vk_id=101),7,eta)
   session.query.assert_not_called()

class ReservationCompatibility(unittest.TestCase):
 def test_save_eta_delegates_before_acquiring_candidate_lock(self):
  snapshot=NS(id=7,status='queued',parallel_offer_driver_id=1,eta_offer_source='parallel')
  session=Mock();session.get.return_value=snapshot;take=Mock()
  scope=dict(Order=Order,PARALLEL_CANDIDATE_STATUSES=('queued',),take=take,vk=Mock())
  load('bot/parallel_orders.py','save_eta',scope)(session,NS(id=1),7,10)
  session.query.assert_not_called()
  take.assert_called_once_with(session,unittest.mock.ANY,7,selected_eta=10)

 def test_retiring_offer_clears_source_and_deadline(self):
  order=NS(eta_offer_source='parallel_auto',eta_offer_expires_at=NOW,
   parallel_offer_driver_id=1,parallel_offer_trip_id=6,parallel_offer_outbox_id=77,
   parallel_notified_driver_ids='{"1":77}')
  load('bot/parallel_orders.py','_clear_route_offer_state')(order)
  self.assertTrue(all(value is None for value in vars(order).values()))

 def test_direct_parallel_offer_is_inline_eta_not_early_acceptance(self):
  from test_v188_eta_blacklist import keyboards,actions
  kb=keyboards();raw=kb.route_parallel_offer_keyboard(7)
  import json
  self.assertTrue(json.loads(raw)['inline'])
  commands=[p['cmd'] for label,p in actions(raw)]
  self.assertIn('parallel_eta',commands);self.assertIn('parallel_eta_custom',commands)
  self.assertNotIn('route_parallel_take',commands)
  self.assertLessEqual(len(json.loads(raw)['buttons']),6)

if __name__=='__main__':unittest.main(verbosity=2)
