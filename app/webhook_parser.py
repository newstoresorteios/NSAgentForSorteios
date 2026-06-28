from __future__ import annotations

from typing import Any

from .audio_service import extract_audio_attachment, is_audio_attachment
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


def _extract_last_visitor_message(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return {}

    for message in reversed(messages):
        if isinstance(message, dict) and message.get("type") == "visitor":
            return message
    return {}


def _extract_visitor(payload: dict[str, Any]) -> dict[str, Any]:
    visitor = payload.get("visitor")
    return visitor if isinstance(visitor, dict) else {}


def should_skip_auto_reply(payload: dict[str, Any]) -> bool:
    """Skip when the latest message in a Conversations fragment is not from the visitor."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False

    last = messages[-1]
    if not isinstance(last, dict):
        return False

    if last.get("type") != "visitor":
        return True

    return bool(last.get("isPushed") or last.get("isTrigger"))


def parse_brevo_whatsapp_payload(payload: dict[str, Any]) -> IncomingMessage:
    """Parse Brevo/WhatsApp-like webhook payloads defensively.

    Supports simplified test payloads and Brevo Conversations webhooks
    (`conversationStarted`, `conversationFragment`).
    """
    message_obj = _extract_from_messages_array(payload)
    visitor_obj = _extract_visitor(payload)
    last_visitor_message = _extract_last_visitor_message(payload)

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
        last_visitor_message.get("text"),
        _get_nested(payload, "message", "text") if isinstance(payload.get("message"), dict) else None,
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
        _get_nested(visitor_obj, "attributes", "SMS"),
        _get_nested(visitor_obj, "attributes", "WHATSAPP"),
        _get_nested(visitor_obj, "contactAttributes", "SMS"),
        _get_nested(visitor_obj, "contactAttributes", "WHATSAPP"),
        _get_nested(visitor_obj, "formattedAttributes", "SMS"),
        message_obj.get("from") if isinstance(message_obj, dict) else None,
    )

    sender_name = _first_non_empty(
        payload.get("name"),
        payload.get("senderName"),
        payload.get("sender_name"),
        _get_nested(payload, "contact", "name"),
        _get_nested(payload, "sender", "name"),
        _get_nested(visitor_obj, "displayedName"),
        _get_nested(visitor_obj, "attributes", "FIRSTNAME"),
        _get_nested(visitor_obj, "integrationAttributes", "FIRSTNAME"),
        _get_nested(message_obj, "profile", "name"),
    )

    visitor_id = _first_non_empty(
        payload.get("visitorId"),
        visitor_obj.get("id"),
    )

    audio_file = extract_audio_attachment(payload)
    input_modality = "text"
    audio_url = None
    audio_mime_type = None
    audio_filename = None
    if audio_file:
        input_modality = "audio"
        audio_url = audio_file.get("link")
        audio_mime_type = audio_file.get("mimeType")
        audio_filename = audio_file.get("name")

    if not text.strip() and audio_file and not is_audio_attachment(audio_file):
        text = _first_non_empty(audio_file.get("name")) or text

    return IncomingMessage(
        provider="brevo",
        event_type=_first_non_empty(
            payload.get("eventName"),
            payload.get("event"),
            payload.get("type"),
            payload.get("eventType"),
        ),
        message_id=_first_non_empty(
            payload.get("id"),
            payload.get("messageId"),
            payload.get("message_id"),
            last_visitor_message.get("id"),
            message_obj.get("id") if isinstance(message_obj, dict) else None,
        ),
        conversation_id=_first_non_empty(
            payload.get("conversationId"),
            payload.get("conversation_id"),
            payload.get("threadId"),
            visitor_obj.get("threadId"),
        ),
        visitor_id=visitor_id,
        sender_phone=sender_phone,
        sender_name=sender_name,
        text=text,
        input_modality=input_modality,
        audio_url=audio_url,
        audio_mime_type=audio_mime_type,
        audio_filename=audio_filename,
        raw=payload,
    )
