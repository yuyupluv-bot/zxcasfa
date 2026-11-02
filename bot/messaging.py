"""Relay chat + media between passenger and driver during an active order.

Each forwarded message is prefixed with a link to the sender's VK profile
(@idXXX) and stored in the `messages` table for the admin audit trail.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from common.models import ROLE_DISPATCHER, Message, Order, User

from . import keyboards as kb
from .vk_client import vk
from .states_service import States, get_state, set_state


def _counterpart(order: Order, sender: User, session: Session) -> User | None:
    """Only participants of this exact ride may relay to one another."""
    if sender.id == order.passenger_id:
        driver_id = order.driver_id or order.parallel_driver_id
        return session.get(User, driver_id) if driver_id else None
    if sender.id in (order.driver_id, order.parallel_driver_id):
        return session.get(User, order.dispatcher_id or order.passenger_id)
    if order.dispatcher_id and sender.id == order.dispatcher_id:
        driver_id = order.driver_id or order.parallel_driver_id
        return session.get(User, driver_id) if driver_id else None
    return None


def relay(
    session: Session,
    order: Order,
    sender: User,
    text: str,
    raw_attachments: list[dict] | None = None,
    keyboard: str | None = None,
) -> bool:
    """Forward text + attachments to the other participant.

    Returns True on success, False if there is no counterpart yet.
    """
    if order.status not in ("assigned", "arrived", "in_progress", "parallel_assigned"):
        return False
    target = _counterpart(order, sender, session)
    if target is None:
        return False

    sender_label = f"[id{sender.vk_id}|{sender.full_name or ('id' + str(sender.vk_id))}]"
    if order.dispatcher_id and sender.id in (order.driver_id, order.parallel_driver_id):
        prefix = f"✉️ По заявке #{order.number} написал водитель {sender_label}:\n"
        keyboard = kb.dispatcher_reply_keyboard(order.id, order.number)
    elif order.dispatcher_id and sender.id == order.dispatcher_id:
        prefix = f"✉️ Ответ диспетчера по заявке #{order.number}:\n"
    else:
        prefix = f"✉️ Сообщение от {sender_label}:\n"

    attachment_str = None
    stored_atts = ""
    media_note = ""
    if raw_attachments:
        reuploaded = vk.reupload_attachments(target.vk_id, raw_attachments)
        if reuploaded:
            attachment_str = ",".join(reuploaded)
            stored_atts = attachment_str
        else:
            # A photo sent without any text must never be silently lost. When
            # VK refuses both the re-upload and the original reference, pass
            # the direct image links instead.
            links = []
            for att in raw_attachments:
                kind = att.get("type")
                if kind == "photo":
                    url = vk.photo_source_url(att.get("photo") or {})
                elif kind == "audio_message":
                    url = vk._voice_url(att.get("audio_message") or {})
                elif kind == "doc":
                    doc = att.get("doc") or {}
                    url = vk._voice_url(doc) if int(doc.get("type") or 0) == 5 else None
                else:
                    url = None
                if url:
                    links.append(url)
            if links:
                media_note = "\n" + "\n".join(links)
                stored_atts = ",".join(links)

    vk.send_message(
        peer_id=target.vk_id,
        text=prefix + (text or "") + media_note,
        attachment=attachment_str,
        keyboard=keyboard,
    )
    # Do not overwrite an input state just because the other side sent a chat
    # message. In particular, a delivery driver may still be entering the
    # price: replacing D_DELIVERY_PRICE with D_CHAT made the amount go back to
    # the passenger as chat text and broke the delivery flow.
    current_state = get_state(session, target.vk_id).state
    protected_input_states = {
        States.D_DELIVERY_PRICE,
        States.D_ETA,
        States.D_ETA_ADD,
        States.D_FINISH_PRICE,
        States.D_PARALLEL_ETA,
        States.D_PARALLEL_ETA_ADD,
        States.P_DELIVERY_CONFIRM,
        States.D_ETA_MENU,
        States.D_CHAT_ORDER_WAIT,
        States.P_CHAT_ORDER_CONFIRM,
    }
    if target.role != ROLE_DISPATCHER and current_state not in protected_input_states:
        # For neutral ride states, keep the convenient immediate-reply mode.
        target_state = (
            States.D_CHAT
            if target.id in (order.driver_id, order.parallel_driver_id)
            else States.P_CHAT
        )
        set_state(session, target.vk_id, target_state, {"order_id": order.id})

    session.add(
        Message(
            order_id=order.id,
            sender_id=sender.id,
            text=text,
            attachments=stored_atts or None,
        )
    )
    return True
