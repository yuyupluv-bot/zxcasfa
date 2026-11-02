"""Read-only driver projection. Mutations are executed by the BOT, never Flask.

The mobile client receives only its own driver's state and action whitelist.
Existing VK handlers remain the authority for validation and business timers.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from common.models import User, Order, State, OutboxMessage, ScheduledJob, DriverQueue, Setting, DriverAppCommand
from common import time_utils

COMMANDS = frozenset('''driver_online driver_offline driver_away choose_line set_line leave_line stay_line change_line queue who_online driver_car car_edit car_back driver_gender set_driver_gender driver_settings driver_settings_back accept decline decline_reason decline_back set_eta eta_pick eta_custom eta_add_menu eta_add eta_add_custom arrived seated bought finish driver_cancel_active driver_cancel_back driver_cancel_no_show driver_cancel_customer driver_cancel_car waiting_start waiting_stop parallel_orders parallel_back parallel_take route_parallel_take parallel_route_decline parallel_eta parallel_eta_custom parallel_eta_add parallel_eta_add_custom parallel_decline pending_orders pending_take chat_take chat_depart chat_add booking_take booking_depart booking_driver_cancel bookings_taken skip_rate_passenger rate_passenger skip propose_price'''.split())
INPUT_HINTS = {
 'driver_eta':'Через сколько минут подъедете?', 'driver_eta_add':'Сколько минут добавить?',
 'driver_finish_price':'Итоговая стоимость поездки, ₽', 'driver_delivery_price':'Стоимость доставки, ₽',
 'driver_custom_price':'Предложите стоимость, ₽', 'driver_parallel_eta':'Время подачи параллельной заявки, мин',
 'driver_parallel_eta_add':'Добавить к подаче, мин', 'driver_rate_passenger_text':'Комментарий к оценке',
 'driver_car_model':'Марка и модель автомобиля', 'driver_car_color':'Цвет автомобиля',
 'driver_car_number':'Государственный номер',
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def iso(value):
    if value is None: return None
    if value.tzinfo is None: value=value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()

def stamp(value):
    return dt.datetime.fromisoformat(value) if value else None

def keyboard_actions(raw):
    try: rows=json.loads(raw or '{}').get('buttons', [])
    except (ValueError,TypeError,AttributeError): return []
    result=[]
    for row in rows:
        for button in row:
            action=button.get('action', {})
            payload=action.get('payload', {})
            if isinstance(payload,str):
                try: payload=json.loads(payload)
                except ValueError: continue
            if not isinstance(payload,dict) or payload.get('cmd') not in COMMANDS: continue
            result.append({'label':str(action.get('label','Действие'))[:80], 'payload':payload,
                           'danger':button.get('color')=='negative'})
    return result

def project(session, driver):
    now=time_utils.now()
    state=session.query(State).filter(State.vk_id==driver.vk_id).one_or_none()
    current_state=state.state if state else 'driver_menu'
    orders=session.query(Order).filter(
        ((Order.driver_id==driver.id) & Order.status.in_(('assigned','arrived','in_progress','parallel_assigned'))) |
        ((Order.offered_driver_id==driver.id) & (Order.status=='searching')) |
        ((Order.parallel_driver_id==driver.id) & (Order.status=='parallel_assigned')) |
        ((Order.parallel_offer_driver_id==driver.id) & Order.status.in_(('created','searching','chat_search')))
    ).order_by(Order.id.asc()).all()
    serialized=[]
    for order in orders:
        serialized.append({'id':order.id,'number':order.number,'status':order.status,'kind':order.order_type,
          'from':order.address_from or '', 'to':order.address_to or '', 'route':order.route_text or '',
          'comment':order.comment or '', 'price':str(order.agreed_price if order.agreed_price is not None else order.customer_price or ''),
          'offered':order.offered_driver_id==driver.id,'parallel':order.parallel_driver_id==driver.id,
          'route_offer':order.parallel_offer_driver_id==driver.id,'arrival_eta':order.arrival_eta,
          'accepted_at':iso(order.driver_accept_time),'arrived_at':iso(order.arrived_at),
          'waiting_started_at':iso(order.waiting_started_at),'waiting_seconds':order.waiting_seconds or 0,
          'voice_in_vk':bool(order.voice_attachment)})
    ids=[o.id for o in orders]
    timers=[]
    if ids:
        for job in session.query(ScheduledJob).filter(ScheduledJob.object_id.in_(ids),ScheduledJob.status=='pending').all():
            if job.kind in ('accept','waiting','prearrival','parallel_eta','route_parallel_offer','eta'):
                timers.append({'kind':job.kind,'order_id':job.object_id,'deadline':iso(job.run_at),'due':stamp(iso(job.run_at))<=stamp(iso(now))})
    messages=session.query(OutboxMessage).filter(OutboxMessage.peer_id==driver.vk_id,
        OutboxMessage.status.notin_(('cancelled','delete_requested','deleting','cancel_requested')),
        OutboxMessage.created_at>=now-dt.timedelta(days=2)).order_by(OutboxMessage.id.desc()).limit(20).all()
    actions=[];seen=set();regular_seen=False
    # Only the latest persistent keyboard; inline controls must reference a
    # current order. An old rating/chat card must not become a new action.
    for message in messages:
        if not message.keyboard: continue
        try: data=json.loads(message.keyboard)
        except (ValueError,TypeError): continue
        inline=bool(data.get('inline'))
        if not inline and regular_seen: continue
        if not inline: regular_seen=True
        for action in keyboard_actions(message.keyboard):
            payload=action['payload'];oid=payload.get('order_id')
            try:oid=int(oid) if oid is not None else None
            except (ValueError,TypeError):continue
            if inline and (not oid or (int(oid) not in ids and payload.get('cmd') not in ('parallel_take','pending_take','chat_take','booking_take','booking_depart','booking_driver_cancel'))): continue
            if oid and payload.get('cmd') not in ('booking_take','booking_depart','booking_driver_cancel','parallel_take','pending_take','chat_take') and int(oid) not in ids:
                # Post-ride rating/skip remains valid only while its pointer is open.
                if payload.get('cmd') not in ('rate_passenger','skip_rate_passenger'): continue
                rating=session.get(Order,int(oid))
                if not rating or rating.driver_id!=driver.id or not rating.driver_rating_prompt_outbox_id: continue
            key=canonical(payload)
            if key not in seen: seen.add(key);actions.append(action)
    def add(label,cmd,**kw):
        payload={'cmd':cmd,**kw};key=canonical(payload)
        if key not in seen:seen.add(key);actions.append({'label':label,'payload':payload,'danger':False})
    offer=next((o for o in orders if o.offered_driver_id==driver.id),None)
    if offer and current_state!='driver_decline_reason':
        add('Принять заявку','accept',order_id=offer.id);add('Отказаться','decline',order_id=offer.id)
    if not orders and current_state in ('driver_menu','driver_on_line','main_menu','start'):
        add('Вернуться на линию' if driver.driver_status=='away' else 'На линию','driver_online')
        add('Выбрать линию','choose_line');add('Очередь','queue')
        if driver.is_on_line:add('Отлучиться','driver_away');add('Сойти с линии','driver_offline')
    from bot.queue_service import driver_line_rank
    queue_rank=driver_line_rank(session,driver)
    heartbeat=session.query(Setting).filter(Setting.key=='driver_app_worker_heartbeat').one_or_none()
    try:online=(now-stamp(heartbeat.value)).total_seconds()<25 if heartbeat else False
    except (ValueError,TypeError):online=False
    permitted=driver.role=='driver' and driver.has_role('driver') and not driver.is_blocked
    if not permitted:actions=[]
    deadlines={t['order_id'] for t in timers if t['kind']=='accept' and t['due']}
    actions=[a for a in actions if not(a['payload'].get('cmd')=='accept' and a['payload'].get('order_id') in deadlines)]
    version_data={'state':current_state,'data':state.data if state else '{}',
      'driver':[driver.role,driver.driver_status,driver.is_on_line,driver.current_line],
      'orders':serialized,'timers':timers,'actions':actions,
      'message_id':messages[0].id if messages else None}
    revision=hashlib.sha256(canonical(version_data).encode()).hexdigest()
    recent=session.query(DriverAppCommand).filter(DriverAppCommand.driver_id==driver.id).order_by(DriverAppCommand.id.desc()).limit(4).all()
    return {'revision':revision,'server_time':iso(now),'bot_online':online,'can_act':permitted and online,
      'driver':{'name':driver.full_name or 'Водитель','status':driver.driver_status,'online':driver.is_on_line,
                'line':driver.current_line,'queue_position':queue_rank},
      'state':current_state,'orders':serialized,'timers':timers,'actions':actions,
      'vk_url':os.getenv('VK_BOT_URL','https://vk.me'),
      'notice':messages[0].text if messages else 'Выберите линию для начала работы.',
      'input_hint':INPUT_HINTS.get(current_state) if permitted else None,
      'role_notice':None if permitted else 'Переключитесь в роль водителя в VK и проверьте доступ.',
      'commands':[{'request_id':c.request_id,'status':c.status,'error':c.error} for c in recent]}
