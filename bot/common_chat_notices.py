"""Notices for passenger messages in the configured rotating-posts VK chat."""
from __future__ import annotations

import json

from common.config import config
from common.logger import get_logger
from common.models import CommonChatNotice, User, ROLE_ADMIN, ROLE_DISPATCHER, ROLE_DRIVER
from common.settings_service import get_int
from . import outbox_service
from .vk_client import vk

log = get_logger("bot.common_chat_notices")
CHAT_LINK = "https://vk.me/join/pC793wArlHG1jPEeHjm9hhsbL2Ibja8CXI0="
NOTICE = "❗ Заявка в общем чате: " + CHAT_LINK


def _has_role(user, role):
    return bool(user and (user.has_role(role) or user.role == role))


def handle_message(session, message):
    """Send per-message notices and retire them on a driver's explicit reply.

    Only the existing rotating-posts peer is observed. An unknown author is a
    passenger; no chat contents are copied into direct messages or the DB.
    """
    peer = int(message.get("peer_id") or 0)
    sender = int(message.get("from_id") or 0)
    if peer < 2_000_000_000 or peer != get_int(session, "rotating_posts_peer_id", 0) or sender <= 0:
        return
    if message.get("action"):
        return
    user = session.query(User).filter(User.vk_id == sender).one_or_none()
    is_driver = _has_role(user, ROLE_DRIVER)
    is_staff = (is_driver or _has_role(user, ROLE_DISPATCHER) or
                _has_role(user, ROLE_ADMIN) or sender in config.ADMIN_VK_IDS)
    reply = message.get("reply_message") or {}
    if is_driver and isinstance(reply, dict):
        cmid = int(reply.get("conversation_message_id") or 0)
        mid = int(reply.get("id") or 0)
        query = session.query(CommonChatNotice).filter(CommonChatNotice.peer_id == peer)
        notice = None
        if cmid:
            notice = query.filter(CommonChatNotice.conversation_message_id == cmid).with_for_update().one_or_none()
        if notice is None and mid:
            notice = query.filter(CommonChatNotice.vk_message_id == mid).with_for_update().first()
        if notice and not notice.answered:
            for outbox_id in json.loads(notice.outbox_ids or "[]"):
                outbox_service.cancel_or_delete(session, int(outbox_id))
            notice.answered = True
        return
    if is_staff:
        return
    cmid = int(message.get("conversation_message_id") or 0)
    mid = int(message.get("id") or 0)
    if not cmid and not mid:
        log.warning("Cannot track common-chat message without a VK ID: peer=%s", peer)
        return
    # Negative IDs are reserved for messages lacking conversation_message_id.
    key = cmid or -mid
    existing = session.query(CommonChatNotice.id).filter(
        CommonChatNotice.peer_id == peer, CommonChatNotice.conversation_message_id == key
    ).first()
    if existing:
        return
    notice = CommonChatNotice(peer_id=peer, conversation_message_id=key,
                              vk_message_id=mid or None, outbox_ids="[]")
    session.add(notice)
    session.flush()
    ids = []
    drivers = session.query(User).filter(User.is_on_line.is_(True), User.is_blocked.is_(False)).all()
    for driver in drivers:
        if not _has_role(driver, ROLE_DRIVER):
            continue
        outbox_id = vk.send_tracked_message(driver.vk_id, NOTICE)
        if outbox_id:
            ids.append(int(outbox_id))
        else:
            log.error("Could not queue common-chat alert for driver=%s", driver.id)
    notice.outbox_ids = json.dumps(ids)
