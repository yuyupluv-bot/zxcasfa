"""Authenticated, bounded work API. Never execute VK handlers in web workers."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import secrets
import uuid
from flask import Blueprint, request, jsonify, abort
from sqlalchemy.exc import IntegrityError
from common.models import DriverPushDevice, User, DriverAppCommand
from common.driver_app_state import project,canonical
from common import time_utils
bp=Blueprint('driver_work',__name__)


def authenticate():
    if request.content_length and request.content_length>8192:abort(413)
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict):abort(400)
    try:id=int(payload.get('device_id',0))
    except (ValueError,TypeError):abort(401)
    secret=payload.get('device_secret','')
    if not isinstance(secret,str) or not 20<=len(secret)<=256:abort(401)
    device=request.db.get(DriverPushDevice,id)
    digest=hashlib.sha256(secret.encode()).hexdigest()
    if not device or not device.device_secret_hash or not secrets.compare_digest(digest,device.device_secret_hash):abort(401)
    driver=request.db.get(User,device.driver_id)
    # is_active is the PUSH-token flag, not an account revocation flag.
    if not driver or not driver.has_role('driver') or driver.is_blocked:abort(403)
    return payload,device,driver

@bp.after_request
def private(response):
    response.headers['Cache-Control']='no-store'
    return response

@bp.post('/api/driver-app/work/state')
def state():
    _,_,driver=authenticate()
    return jsonify(project(request.db,driver))

@bp.post('/api/driver-app/work/action')
def action():
    body,device,driver=authenticate()
    try:request_id=str(uuid.UUID(str(body.get('request_id',''))))
    except ValueError:abort(400)
    revision=body.get('revision','')
    if not isinstance(revision,str) or len(revision)!=64:abort(400)
    payload=body.get('payload',{});text=body.get('text','')
    if not isinstance(payload,dict) or not isinstance(text,str) or len(text)>500:abort(400)
    if bool(payload)==bool(text.strip()):abort(400)
    stored={'payload':payload,'text':text}
    digest=hashlib.sha256(canonical(stored).encode()).hexdigest()
    prior=request.db.query(DriverAppCommand).filter_by(driver_id=driver.id,request_id=request_id).one_or_none()
    if prior:
        if prior.body_hash!=digest:return jsonify(error='request_id_reused'),409
        return jsonify(request_id=request_id,status=prior.status,error=prior.error)
    snapshot=project(request.db,driver)
    if not snapshot['can_act']:return jsonify(error='bot_unavailable_or_wrong_role'),503
    if snapshot['revision']!=revision:return jsonify(error='stale_state'),409
    if payload and canonical(payload) not in {canonical(a['payload']) for a in snapshot['actions']}:abort(403)
    if text and not snapshot['input_hint']:abort(403)
    # A slow/offline device cannot create an unbounded queue.
    if request.db.query(DriverAppCommand.id).filter(DriverAppCommand.driver_id==driver.id,
        DriverAppCommand.status.in_(('pending','running'))).count()>=3:
        return jsonify(error='action_pending'),429
    now=time_utils.now()
    row=DriverAppCommand(driver_id=driver.id,device_id=device.id,request_id=request_id,
        revision=revision,body_hash=digest,body=canonical(stored),created_at=now,
        expires_at=now+dt.timedelta(seconds=20),status='pending')
    try:request.db.add(row);request.db.commit()
    except IntegrityError:
        request.db.rollback()
        prior=request.db.query(DriverAppCommand).filter_by(driver_id=driver.id,request_id=request_id).one_or_none()
        if not prior or prior.body_hash!=digest:return jsonify(error='request_conflict'),409
        return jsonify(request_id=request_id,status=prior.status,error=prior.error)
    return jsonify(request_id=request_id,status='pending'),202
