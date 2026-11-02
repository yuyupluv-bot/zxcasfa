"""Firebase push notifications for the optional Android driver companion."""
from __future__ import annotations

import base64
import json
import os

from common.logger import get_logger
from common.models import DriverPushDevice, PushScenario

log = get_logger("bot.push")
DEFAULTS = {
    "new_order": ("Новая заявка", 60),
    "customer_price": ("Заявка со своей ценой", 30),
    "dispatcher_order": ("Заявка диспетчера", 60),
    "parallel_order": ("Параллельная заявка", 60),
    "cancelled": ("Заявка отменена", 20),
}


def ensure_scenarios(session):
    for key, (title, duration) in DEFAULTS.items():
        if not session.query(PushScenario).filter_by(event_key=key).first():
            session.add(PushScenario(
                event_key=key,
                title=title,
                duration_seconds=duration,
            ))


def _firebase():
    """Initialize Firebase from Portainer env or a mounted JSON file.

    FIREBASE_SERVICE_ACCOUNT_B64 is preferred for installations that only have
    hosting panel access. It contains the base64-encoded service-account JSON and
    avoids committing credentials to Git or writing into a container.
    """
    try:
        import firebase_admin
        from firebase_admin import credentials

        if firebase_admin._apps:
            return firebase_admin

        encoded = (os.getenv("FIREBASE_SERVICE_ACCOUNT_B64") or "").strip()
        raw_json = (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
        source = ""
        certificate = None
        if encoded:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            certificate = credentials.Certificate(json.loads(decoded))
            source = "FIREBASE_SERVICE_ACCOUNT_B64"
        elif raw_json:
            certificate = credentials.Certificate(json.loads(raw_json))
            source = "FIREBASE_SERVICE_ACCOUNT_JSON"
        else:
            path = os.getenv(
                "FIREBASE_SERVICE_ACCOUNT_FILE",
                "/app/secrets/firebase-service-account.json",
            )
            if os.path.isfile(path) and os.access(path, os.R_OK):
                certificate = credentials.Certificate(path)
                source = path

        if certificate is None:
            log.error(
                "Firebase credentials are missing: set "
                "FIREBASE_SERVICE_ACCOUNT_B64 in BotHost/Portainer or mount %s",
                os.getenv(
                    "FIREBASE_SERVICE_ACCOUNT_FILE",
                    "/app/secrets/firebase-service-account.json",
                ),
            )
            return None

        firebase_admin.initialize_app(certificate, options={"httpTimeout": 15})
        log.info("Firebase initialized from %s", source)
        return firebase_admin
    except Exception:
        log.exception("Firebase initialization failed")
        return None


def send(session, driver_id: int, event_key: str, order=None):
    """Send only to active devices paired with this exact driver."""
    if not driver_id:
        return 0
    ensure_scenarios(session)
    scenario = session.query(PushScenario).filter_by(event_key=event_key).first()
    if not scenario or not scenario.enabled:
        log.info("Push skipped: scenario disabled event=%s", event_key)
        return 0
    devices = session.query(DriverPushDevice).filter_by(
        driver_id=int(driver_id),
        is_active=True,
    ).all()
    if not devices:
        log.warning(
            "Push skipped: no active device driver=%s event=%s",
            driver_id,
            event_key,
        )
        return 0
    app = _firebase()
    if app is None:
        return 0

    from firebase_admin import messaging

    route = (getattr(order, "route_text", None) or "") if order else ""
    data = {
        "event": event_key,
        "title": scenario.title,
        "order_id": str(getattr(order, "id", "") or ""),
        "order_number": str(getattr(order, "number", "") or ""),
        "route": route,
        "price": str(getattr(order, "customer_price", "") or ""),
        "duration": str(scenario.duration_seconds),
        "repeat": str(scenario.repeat_seconds),
        "vibrate": "1" if scenario.vibrate else "0",
        "sound_url": scenario.sound_file or "",
        "vk_url": os.getenv("VK_BOT_URL", "https://vk.me/club0"),
    }
    sent = 0
    for device in devices:
        try:
            messaging.send(messaging.Message(
                token=device.token,
                data=data,
                android=messaging.AndroidConfig(priority="high"),
            ))
            sent += 1
        except messaging.UnregisteredError:
            device.is_active = False
            log.warning("Disabled unregistered Firebase device=%s", device.id)
        except Exception:
            log.exception("Push failed device=%s", device.id)
    log.info(
        "Push result driver=%s event=%s devices=%s sent=%s",
        driver_id,
        event_key,
        len(devices),
        sent,
    )
    return sent
