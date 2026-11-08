"""Passenger-request notices for the configured VK posts chat.

Classification is deliberately conservative: not every chat message is a ride.
Deletion reconciliation is best-effort because Bots Long Poll has no documented
incoming-message-deleted event. Never infer deletion from a failed VK request.
"""
from __future__ import annotations

import json
import re
import threading
import time

from common.config import config
from common.database import session_scope
from common.logger import get_logger
from common.models import CommonChatNotice, User, ROLE_ADMIN, ROLE_DISPATCHER, ROLE_DRIVER
from common.settings_service import get_int
from . import outbox_service
from .vk_client import vk

log = get_logger("bot.common_chat_notices")
CHAT_LINK = "https://vk.me/join/pC793wArlHG1jPEeHjm9hhsbL2Ibja8CXI0="
NOTICE = "❗ Заявка в общем чате: " + CHAT_LINK
POLL_SECONDS = 30
_misses: dict[tuple[int, int], int] = {}
_worker_lock = threading.Lock()
_worker_started = False


def _has_role(user, role):
    return bool(user and (user.has_role(role) or user.role == role))


def is_price_question(text: str) -> bool:
    value = ' '.join((text or '').casefold().replace('ё', 'е').split())
    if not value:
        return False
    if re.search(r'\b(?:сколько\s+(?:будет\s+)?сто\w*|сколько\s+(?:будет\s+)?по\s+(?:цене|стоимости)|почем|какая\s+(?:будет\s+)?(?:цена|стоимость)|какова\s+(?:цена|стоимость)|почему\b.{0,40}\b(?:дорого|дороговато|дороговизн\w*|цена|стоим\w*)|дорого|дороговато|дороговизн\w*)\b', value):
        return True
    return bool(re.search(r'\b(?:цен[аыеуы]|стоимость|тариф\w*)\b', value) and
                ('?' in value or re.search(r'\b(?:какая|какой|какую|сколько|почему|подскажите|скажите|узнать)\b', value)))


def is_cancellation(text: str) -> bool:
    value = ' '.join((text or '').casefold().replace('ё', 'е').split())
    return bool(re.search(r'\b(?:отмена|отменяю|отмените|отменил[аи]?|уже\s+не\s+(?:надо|нужно|актуально)|не\s+актуально|не\s+надо|передумал[аи]?)\b', value))


def is_ride_request(text: str) -> bool:
    value = ' '.join((text or '').casefold().replace('ё', 'е').split())
    if not value or is_cancellation(value):
        return False
    # Do not misclassify follow-ups that happen to contain a street or number.
    followup = re.match(r'^(?:жду|ожидаю|спасибо|благодарю|привет|здравствуйте|добрый\s+(?:день|вечер|утро)|уже\s+еду|выхожу|я\s+на\s+месте)\b', value)
    explicit = re.search(r'\b(?:такси|таксист\w*|заказ\w*|заявк\w*|машин(?:а|у|ку|ы|ой|е)|авто|водител\w*|отвез\w*|довез\w*|подвез\w*|забер\w*|подайте|вызов\w*)\b', value)
    demand = re.search(r'\b(?:нуж(?:но|на|ен)|надо|хочу|ищу|можно|едем|поедем|поехать|поездк\w*|возьмите)\b', value)
    if is_price_question(value) and not (explicit and demand):
        return False
    if followup and not (explicit and demand):
        return False
    if re.search(r'\b(?:машин\w*|водител\w*|такси)\b.{0,30}\b(?:приехал\w*|подъехал\w*|на\s+месте|уже\s+едет)\b', value) and not demand:
        return False
    if explicit:
        return True
    if re.search(r'\bпо\s+(?:паши(?:и|я|ю|е)|кусь(?:е|я|и|ю))\b', value):
        return True
    if re.search(r'\b(?:из|от|с)\s+\S+.{0,80}\b(?:до|в|на)\s+\S+', value):
        return True
    if re.search(r'\S\s+[—–-]\s*\S', value) and not followup:
        return True
    if re.search(r'\b(?:ул(?:ица|\.)?|проспект|переулок|дом|д\.)\s*[а-яa-zё\- ]+\s+\d+', value):
        return True
    if re.search(r'\b(?!(?:цена|стоимость|тариф|пассажир\w*|человек|людей|минут\w*|час\w*|рубл\w*)\b)[а-яё][а-яё-]{2,}\s+\d+[а-яё]?\b', value):
        return True
    if re.search(r'\b(?:паши\w*|кусь\w*|горнозаводск\w*|чусов\w*|саран\w*)\b', value):
        cities = re.findall(r'\b(?:паши\w*|кусь\w*|горнозаводск\w*|чусов\w*|саран\w*)\b', value)
        return len(cities) >= 2 or bool(demand)
    return bool(demand and re.search(r'\b(?:до|из|на|в)\s+[а-яa-zё]', value))


def notify_price_question(session, message):
    """Notify all bot admins for fare questions in DM or the configured chat."""
    sender = int(message.get('from_id') or 0)
    peer = int(message.get('peer_id') or 0)
    text = str(message.get('text') or '').strip()
    chat_peer = get_int(session, 'rotating_posts_peer_id', 0)
    if sender <= 0 or message.get('action') or not (peer == sender or (peer >= 2_000_000_000 and peer == chat_peer)) or not is_price_question(text):
        return
    author = session.query(User).filter(User.vk_id == sender).one_or_none()
    name = (author.full_name if author and author.full_name else f'id{sender}').replace('|', ' ').replace(']', ' ')[:150]
    source = 'в общем чате' if peer != sender else 'в личных сообщениях бота'
    body = f'❓ Вопрос о стоимости {source}\nОт: [id{sender}|{name}] (VK ID {sender})\nСообщение: {text[:3000]}'
    if peer != sender:
        body += '\nЧат: ' + CHAT_LINK
    ids = {int(value) for value in config.ADMIN_VK_IDS}
    for candidate in session.query(User).filter(
        (User.role == ROLE_ADMIN) | User.granted_roles.contains(ROLE_ADMIN)
    ).all():
        if _has_role(candidate, ROLE_ADMIN):
            ids.add(int(candidate.vk_id))
    for admin_id in sorted(ids):
        if admin_id > 0:
            if not vk.send_tracked_message(admin_id, body):
                log.warning('Could not queue price question to admin=%s', admin_id)


def _retire(session, notice):
    if notice and not notice.answered:
        for outbox_id in json.loads(notice.outbox_ids or '[]'):
            outbox_service.cancel_or_delete(session, int(outbox_id))
        notice.answered = True


def _reply_notice(session, peer, reply):
    cmid = int(reply.get('conversation_message_id') or 0)
    mid = int(reply.get('id') or 0)
    query = session.query(CommonChatNotice).filter(CommonChatNotice.peer_id == peer)
    notice = query.filter(CommonChatNotice.conversation_message_id == cmid).with_for_update().one_or_none() if cmid else None
    if notice is None and mid:
        notice = query.filter(CommonChatNotice.vk_message_id == mid).with_for_update().first()
    return notice


def handle_message(session, message):
    peer = int(message.get('peer_id') or 0)
    sender = int(message.get('from_id') or 0)
    if peer < 2_000_000_000 or peer != get_int(session, 'rotating_posts_peer_id', 0) or sender <= 0 or message.get('action'):
        return
    user = session.query(User).filter(User.vk_id == sender).one_or_none()
    is_driver = _has_role(user, ROLE_DRIVER)
    is_staff = (is_driver or _has_role(user, ROLE_DISPATCHER) or _has_role(user, ROLE_ADMIN) or sender in config.ADMIN_VK_IDS)
    reply = message.get('reply_message') or {}
    if is_driver and isinstance(reply, dict):
        _retire(session, _reply_notice(session, peer, reply))
        return
    if is_staff:
        return
    text = str(message.get('text') or '').strip()
    if is_cancellation(text):
        notice = _reply_notice(session, peer, reply) if isinstance(reply, dict) else None
        if notice and notice.author_vk_id != sender:
            return
        if notice is None:
            notice = (session.query(CommonChatNotice).filter(CommonChatNotice.peer_id == peer,
                CommonChatNotice.author_vk_id == sender, CommonChatNotice.answered.is_(False))
                .order_by(CommonChatNotice.id.desc()).with_for_update().first())
        _retire(session, notice)
        return
    if not is_ride_request(text):
        return
    cmid = int(message.get('conversation_message_id') or 0)
    mid = int(message.get('id') or 0)
    if not cmid and not mid:
        log.warning('Cannot track common-chat message without a VK ID: peer=%s', peer)
        return
    key = cmid or -mid
    existing = session.query(CommonChatNotice.id).filter(CommonChatNotice.peer_id == peer, CommonChatNotice.conversation_message_id == key).first()
    if existing:
        return
    notice = CommonChatNotice(peer_id=peer, conversation_message_id=key, vk_message_id=mid or None,
                              author_vk_id=sender, outbox_ids='[]')
    session.add(notice)
    session.flush()
    ids = []
    drivers = session.query(User).filter(User.is_on_line.is_(True), User.is_blocked.is_(False)).all()
    for driver in drivers:
        if _has_role(driver, ROLE_DRIVER):
            outbox_id = vk.send_tracked_message(driver.vk_id, NOTICE)
            if outbox_id:
                ids.append(int(outbox_id))
            else:
                log.error('Could not queue common-chat alert for driver=%s', driver.id)
    notice.outbox_ids = json.dumps(ids)


def reconcile_deleted_messages():
    """Poll in bounded VK batches; only retire previously observed messages.

    History supplies a live control message for each request. A failed request,
    missing control, malformed response, or first missing observation is never
    treated as deletion. Deleted-before-first-observation cannot be proven.
    """
    with session_scope() as session:
        rows = session.query(CommonChatNotice.id, CommonChatNotice.peer_id,
                             CommonChatNotice.conversation_message_id).filter(
            CommonChatNotice.answered.is_(False), CommonChatNotice.conversation_message_id > 0
        ).order_by(CommonChatNotice.id).all()
    peers = {}
    for row_id, peer, cmid in rows:
        peers.setdefault(int(peer), []).append((int(row_id), int(cmid)))
    live_keys = {(peer, cmid) for peer, batch in peers.items() for _, cmid in batch}
    for key in list(_misses):
        if key not in live_keys:
            _misses.pop(key, None)
    for peer, records in peers.items():
        try:
            api = vk._message_api()  # rotating chat is owned by the community
            history = api.messages.getHistory(peer_id=peer, count=1)
            items = history.get('items') if isinstance(history, dict) else None
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                continue
            anchor = int(items[0].get('conversation_message_id') or 0)
            if anchor <= 0:
                continue
            for offset in range(0, len(records), 99):
                batch = records[offset:offset + 99]
                ids = list(dict.fromkeys([anchor] + [cmid for _, cmid in batch]))
                result = api.messages.getByConversationMessageId(peer_id=peer, conversation_message_ids=','.join(map(str, ids)))
                returned = result.get('items') if isinstance(result, dict) else None
                if not isinstance(returned, list):
                    continue
                found = {int(item.get('conversation_message_id') or 0) for item in returned
                         if isinstance(item, dict) and not item.get('deleted')}
                if anchor not in found:
                    continue
                with session_scope() as session:
                    locked = session.query(CommonChatNotice).filter(CommonChatNotice.id.in_([i for i, _ in batch]),
                        CommonChatNotice.answered.is_(False)).with_for_update(skip_locked=True).all()
                    for notice in locked:
                        key = (peer, int(notice.conversation_message_id))
                        if key[1] in found:
                            notice.seen_in_vk = True
                            _misses.pop(key, None)
                        elif notice.seen_in_vk:
                            _misses[key] = _misses.get(key, 0) + 1
                            if _misses[key] >= 2:
                                _retire(session, notice)
                                _misses.pop(key, None)
        except Exception as exc:  # VK errors must never cancel notices
            log.warning('Could not check deleted common-chat messages for peer=%s: %s', peer, exc)


def _worker():
    while True:
        try:
            reconcile_deleted_messages()
        except Exception:
            log.exception('Common-chat deletion reconciliation failed')
        time.sleep(POLL_SECONDS)


def start_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name='common-chat-deletion-watch', daemon=True).start()
            _worker_started = True
