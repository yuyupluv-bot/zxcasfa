from __future__ import annotations
import os, secrets, hashlib
import datetime as dt
from pathlib import Path
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename
from common.models import ROLE_DRIVER, DriverPushDevice, PushScenario, DriverAppPairCode, User
from common import time_utils
from bot.push_service import DEFAULTS, ensure_scenarios
bp=Blueprint("push_app",__name__)
UPLOAD=Path(os.getenv("PUSH_SOUND_DIR","/app/data/push_sounds")); UPLOAD.mkdir(parents=True,exist_ok=True)
APK_FILE=Path(os.getenv("DRIVER_APP_APK_FILE","/app/downloads/BEZTAKSY.apk"))
ALLOWED={"mp3","wav","ogg","m4a"}

def _admin():
 if not session.get("admin_id"): abort(401)

@bp.route("/driver-app",methods=["GET","POST"])
def settings():
 _admin(); db=request.db
 ensure_scenarios(db)
 if request.method=="POST":
  action=(request.form.get("action") or "save_scenarios").strip()
  if action=="create_review_code":
   try: driver_id=int(request.form.get("driver_id") or 0)
   except (TypeError,ValueError): driver_id=0
   driver=db.get(User,driver_id) if driver_id else None
   if not driver or not driver.has_role(ROLE_DRIVER):
    flash("Выберите тестового водителя.","danger")
    return redirect(url_for("push_app.settings"))
   db.query(DriverAppPairCode).filter(
    DriverAppPairCode.is_reusable.is_(True),
    DriverAppPairCode.label=="RuStore moderation",
   ).delete(synchronize_session=False)
   for _ in range(20):
    code=f"{secrets.randbelow(10_000_000_000):010d}"
    digest=hashlib.sha256(code.encode("ascii")).hexdigest()
    if not db.query(DriverAppPairCode.id).filter_by(code_hash=digest).first(): break
   else:
    flash("Не удалось создать код. Попробуйте ещё раз.","danger")
    return redirect(url_for("push_app.settings"))
   db.add(DriverAppPairCode(
    driver_id=driver.id,
    code_hash=digest,
    expires_at=time_utils.now()+dt.timedelta(days=30),
    is_reusable=True,
    label="RuStore moderation",
   ))
   db.commit()
   session["rustore_review_code"]=code
   flash("Код RuStore создан. Скопируйте его сейчас: после обновления страницы он будет скрыт.","success")
   return redirect(url_for("push_app.settings"))
  if action=="revoke_review_code":
   removed=db.query(DriverAppPairCode).filter(
    DriverAppPairCode.is_reusable.is_(True),
    DriverAppPairCode.label=="RuStore moderation",
   ).delete(synchronize_session=False)
   db.commit()
   session.pop("rustore_review_code",None)
   flash("Код RuStore отозван." if removed else "Активного кода RuStore нет.","success")
   return redirect(url_for("push_app.settings"))
  for key in DEFAULTS:
   row=db.query(PushScenario).filter_by(event_key=key).one()
   row.duration_seconds=max(1,min(600,int(request.form.get(key+"_duration",row.duration_seconds))))
   row.repeat_seconds=max(1,min(60,int(request.form.get(key+"_repeat",row.repeat_seconds))))
   row.vibrate=request.form.get(key+"_vibrate")=="1"; row.enabled=request.form.get(key+"_enabled")=="1"
   f=request.files.get(key+"_sound")
   if f and f.filename:
    ext=f.filename.rsplit(".",1)[-1].lower()
    if ext not in ALLOWED: abort(400,"Допустимы MP3/WAV/OGG/M4A")
    name=f"{key}-{secrets.token_hex(8)}.{ext}"; f.save(UPLOAD/name); row.sound_file="/driver-app/sounds/"+name
  db.commit(); flash("Настройки уведомлений сохранены","success"); return redirect(url_for("push_app.settings"))
 db.commit(); rows=db.query(PushScenario).order_by(PushScenario.id).all()
 drivers=[user for user in db.query(User).order_by(User.full_name.asc()).all() if user.has_role(ROLE_DRIVER)]
 review_pairing=(
  db.query(DriverAppPairCode)
  .filter(
   DriverAppPairCode.is_reusable.is_(True),
   DriverAppPairCode.label=="RuStore moderation",
   DriverAppPairCode.expires_at>time_utils.now(),
  )
  .order_by(DriverAppPairCode.created_at.desc())
  .first()
 )
 review_code=session.pop("rustore_review_code",None)
 return render_template(
  "driver_app.html",
  scenarios=rows,
  drivers=drivers,
  review_pairing=review_pairing,
  review_code=review_code,
 )


@bp.route("/privacy")
def privacy():
 return render_template("privacy.html")

@bp.route("/driver-app/sounds/<name>")
def sound(name):
 from flask import send_from_directory
 return send_from_directory(UPLOAD,secure_filename(name),as_attachment=False)

def _find_apk_file():
 # Support both the documented repository-level directory and the path used
 # by Android Studio/GitHub uploads. The latter is convenient for Portainer
 # users who have no SSH access to the Docker host.
 candidates = [
  APK_FILE,
  Path("/app/downloads/BEZTAKSY.apk"),
  Path("/app/android-driver-app/app/downloads/BEZTAKSY.apk"),
 ]
 seen = set()
 for candidate in candidates:
  normalized = str(candidate)
  if normalized in seen:
   continue
  seen.add(normalized)
  if candidate.is_file():
   return candidate
 return None


@bp.route("/driver-app/download.apk")
def download_apk():
 from flask import send_file
 apk_file = _find_apk_file()
 if apk_file is None:
  abort(404, "APK ещё не загружен")
 return send_file(
  apk_file,
  mimetype="application/vnd.android.package-archive",
  as_attachment=True,
  download_name="BEZTAKSY.apk",
 )

@bp.route("/api/driver-app/register",methods=["POST"] )
def register():
 payload=request.get_json(silent=True) or {}
 code=str(payload.get("code") or "").strip(); token=str(payload.get("token") or "").strip()
 if not (6<=len(code)<=10) or not code.isdigit(): return jsonify(error="invalid_code"),400
 if not token: return jsonify(error="token_required"),400
 digest=hashlib.sha256(code.encode("ascii")).hexdigest()
 pairing=request.db.query(DriverAppPairCode).filter_by(code_hash=digest,used_at=None).with_for_update().one_or_none()
 now=time_utils.now()
 if not pairing or pairing.expires_at < now: return jsonify(error="invalid_or_expired_code"),403
 # Return a new per-installation credential on every successful pairing.
 # Keeping only its digest server-side makes later FCM token rotation safe
 # without retaining the one-time pairing code on the phone.
 device_secret=secrets.token_urlsafe(32)
 secret_hash=hashlib.sha256(device_secret.encode("utf-8")).hexdigest()
 row=request.db.query(DriverPushDevice).filter_by(token=token).first()
 if row:
  row.driver_id=pairing.driver_id; row.is_active=True
  row.device_secret_hash=secret_hash
 else:
  row=DriverPushDevice(
   driver_id=pairing.driver_id,
   token=token,
   device_secret_hash=secret_hash,
  )
  request.db.add(row)
 request.db.flush()
 if not pairing.is_reusable: pairing.used_at=now
 driver=pairing.driver
 request.db.commit()
 return jsonify(
  ok=True,
  driver=driver.full_name or str(driver.vk_id),
  device_id=row.id,
  device_secret=device_secret,
 )


@bp.route("/api/driver-app/token", methods=["POST"])
def refresh_token():
 payload=request.get_json(silent=True) or {}
 try:
  device_id=int(payload.get("device_id") or 0)
 except (TypeError, ValueError):
  device_id=0
 device_secret=str(payload.get("device_secret") or "").strip()
 token=str(payload.get("token") or "").strip()
 if not device_id or not device_secret or not token:
  return jsonify(error="invalid_request"),400
 digest=hashlib.sha256(device_secret.encode("utf-8")).hexdigest()
 row=(
  request.db.query(DriverPushDevice)
  .filter(DriverPushDevice.id==device_id)
  .with_for_update()
  .one_or_none()
 )
 if not row or not row.device_secret_hash or not secrets.compare_digest(row.device_secret_hash,digest):
  return jsonify(error="invalid_device"),403
 duplicate=(
  request.db.query(DriverPushDevice)
  .filter(DriverPushDevice.token==token,DriverPushDevice.id!=row.id)
  .with_for_update()
  .one_or_none()
 )
 if duplicate:
  request.db.delete(duplicate)
 row.token=token
 row.is_active=True
 row.updated_at=time_utils.now()
 request.db.commit()
 return jsonify(ok=True)


@bp.route("/api/driver-app/unregister", methods=["POST"])
def unregister_device():
 payload=request.get_json(silent=True) or {}
 try:
  device_id=int(payload.get("device_id") or 0)
 except (TypeError,ValueError):
  device_id=0
 device_secret=str(payload.get("device_secret") or "").strip()
 if not device_id or not device_secret:
  return jsonify(error="invalid_request"),400
 digest=hashlib.sha256(device_secret.encode("utf-8")).hexdigest()
 row=(
  request.db.query(DriverPushDevice)
  .filter(DriverPushDevice.id==device_id)
  .with_for_update()
  .one_or_none()
 )
 if not row or not row.device_secret_hash or not secrets.compare_digest(row.device_secret_hash,digest):
  return jsonify(error="invalid_device"),403
 request.db.delete(row)
 request.db.commit()
 return jsonify(ok=True)
