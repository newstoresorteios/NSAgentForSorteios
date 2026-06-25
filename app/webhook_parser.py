from __future__ import annotations
from typing import Any
from .models import IncomingMessage


def _get_nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if not isinstance(value, str):
            text = str(value).strip()
            if text:
                return text
    return None


def _extract_from_messages_array(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            return first
    return {}


def parse_brevo_whatsapp_payload(payload: dict[str, Any]) -> IncomingMessage:
    """Parse Brevo/WhatsApp-like webhook payloads defensively.

    Brevo payloads vary by product and configuration. This parser extracts common
    fields without assuming one exact schema, then keeps the full raw payload for audit.
    """
    message_obj = _extract_from_messages_array(payload)

    text = _first_non_empty(
        payload.get("text"),
        payload.get("message"),
        payload.get("body"),
        payload.get("content"),
        _get_nested(payload, "text", "body"),
        _get_nested(payload, "message", "text"),
        _get_nested(message_obj, "text", "body"),
        message_obj.get("body") if isinstance(message_obj, dict) else None,
        message_obj.get("text") if isinstance(message_obj, dict) else None,
    ) or ""

    sender_phone = _first_non_empty(
        payload.get("sender"),
        payload.get("from"),
        payload.get("phone"),
        payload.get("contactNumber"),
        payload.get("contact_number"),
        _get_nested(payload, "contact", "phone"),
        _get_nested(payload, "contact", "whatsapp"),
        _get_nested(payload, "sender", "phone"),
        _get_nested(payload, "from", "phone"),
        message_obj.get("from") if isinstance(message_obj, dict) else None,
    )

    sender_name = _first_non_empty(
        payload.get("name"),
        payload.get("senderName"),
        payload.get("sender_name"),
        _get_nested(payload, "contact", "name"),
        _get_nested(payload, "sender", "name"),
        _get_nested(message_obj, "profile", "name"),
    )

    return IncomingMessage(
        provider="brevo",
        event_type=_first_non_empty(payload.get("event"), payload.get("type"), payload.get("eventType")),
        message_id=_first_non_empty(payload.get("id"), payload.get("messageId"), payload.get("message_id"), message_obj.get("id") if isinstance(message_obj, dict) else None),
        conversation_id=_first_non_empty(payload.get("conversationId"), payload.get("conversation_id"), payload.get("threadId")),
        sender_phone=sender_phone,
        sender_name=sender_name,
        text=text,
        raw=payload,
    )
