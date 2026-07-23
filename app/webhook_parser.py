from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .audio_service import extract_audio_attachment, is_audio_attachment, is_placeholder_audio_text
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


def _message_type(message: dict[str, Any]) -> str:
    value = _first_non_empty(
        message.get("type"),
        message.get("role"),
        message.get("senderType"),
        message.get("authorType"),
        message.get("direction"),
    )
    return (value or "").lower()


def _is_visitor_message(message: dict[str, Any]) -> bool:
    return _message_type(message) in {"visitor", "client", "customer", "user", "inbound"}


def _message_id(message: dict[str, Any]) -> str | None:
    return _first_non_empty(
        message.get("id"),
        message.get("messageId"),
        message.get("message_id"),
        message.get("uuid"),
    )


def _message_timestamp(message: dict[str, Any]) -> float | None:
    for field in ("createdAt", "created_at", "timestamp", "date", "updatedAt"):
        value = message.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            try:
                return float(text)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    continue
    return None


def select_effective_inbound_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Select the chronologically newest fragment item, regardless of array order."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return {}

    valid_messages = [message for message in messages if isinstance(message, dict)]
    if not valid_messages:
        return {}

    timestamped = [
        (timestamp, index, message)
        for index, message in enumerate(valid_messages)
        if (timestamp := _message_timestamp(message)) is not None
    ]
    if timestamped:
        return max(timestamped, key=lambda item: (item[0], item[1]))[2]
    return valid_messages[-1]


def selected_message_info(payload: dict[str, Any], message: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = message if message is not None else select_effective_inbound_message(payload)
    return {
        "role": _message_type(selected) or None,
        "timestamp_present": _message_timestamp(selected) is not None,
        "ordering_fallback": bool(
            isinstance(payload.get("messages"), list)
            and any(isinstance(item, dict) and _message_timestamp(item) is not None for item in payload.get("messages", [])) is False
        ),
    }


def _extract_audio_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if not message:
        return None

    file_obj = message.get("file")
    if isinstance(file_obj, dict) and is_audio_attachment(file_obj) and file_obj.get("link"):
        return file_obj

    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict) and is_audio_attachment(attachment) and attachment.get("link"):
                return attachment

    return None


def _extract_visitor(payload: dict[str, Any]) -> dict[str, Any]:
    visitor = payload.get("visitor")
    return visitor if isinstance(visitor, dict) else {}


def should_skip_auto_reply(payload: dict[str, Any]) -> bool:
    """Skip when the latest message in a Conversations fragment is not from the visitor."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload.get("eventName") == "conversationFragment"

    selected = select_effective_inbound_message(payload)
    if not selected:
        return False

    if not _is_visitor_message(selected):
        return True

    return bool(selected.get("isPushed") or selected.get("isTrigger"))


def inbound_skip_reason(payload: dict[str, Any]) -> str | None:
    """Explain why a webhook should not enter the agent pipeline."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "no_inbound_message" if payload.get("eventName") == "conversationFragment" else None

    selected = select_effective_inbound_message(payload)
    if not selected:
        return "invalid_payload"
    if not _is_visitor_message(selected):
        message_type = _message_type(selected)
        return "agent_message" if message_type in {"agent", "bot", "assistant"} else "outbound_message"
    if selected.get("isPushed") or selected.get("isTrigger"):
        return "agent_message"
    return None


def webhook_event_skip_reason(payload: dict[str, Any]) -> str | None:
    """Reject webhook events that are not new inbound messages."""
    event_name = _first_non_empty(
        payload.get("eventName"),
        payload.get("event"),
        payload.get("eventType"),
    )
    if event_name == "conversationTranscript":
        return "non_inbound_event"
    return None


def _extract_primary_message(payload: dict[str, Any]) -> dict[str, Any]:
    return select_effective_inbound_message(payload)


def parse_brevo_whatsapp_payload(payload: dict[str, Any]) -> IncomingMessage:
    """Parse Brevo/WhatsApp-like webhook payloads defensively."""
    visitor_obj = _extract_visitor(payload)
    last_visitor_message = _extract_primary_message(payload)

    audio_file = _extract_audio_from_message(last_visitor_message) or extract_audio_attachment(payload)

    message_text = _first_non_empty(
        last_visitor_message.get("text") if isinstance(last_visitor_message.get("text"), str) else None,
        last_visitor_message.get("body") if isinstance(last_visitor_message.get("body"), str) else None,
        _get_nested(last_visitor_message, "text", "body"),
    )
    is_fragment = payload.get("eventName") == "conversationFragment"
    text = _first_non_empty(
        message_text,
        None if is_fragment else payload.get("text"),
        None if is_fragment else payload.get("message"),
        None if is_fragment else payload.get("body"),
        None if is_fragment else payload.get("content"),
        None if is_fragment else _get_nested(payload, "text", "body"),
        None if is_fragment else _get_nested(payload, "message", "text"),
        None if is_fragment else _get_nested(payload, "message", "text") if isinstance(payload.get("message"), dict) else None,
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
        last_visitor_message.get("from"),
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
        _get_nested(last_visitor_message, "profile", "name"),
    )

    visitor_id = _first_non_empty(
        payload.get("visitorId"),
        visitor_obj.get("id"),
    )

    input_modality = "text"
    audio_url = None
    audio_mime_type = None
    audio_filename = None
    if audio_file:
        input_modality = "audio"
        audio_url = audio_file.get("link")
        audio_mime_type = audio_file.get("mimeType")
        audio_filename = audio_file.get("name")
        if is_placeholder_audio_text(text, audio_filename):
            text = ""

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
        message_id=_message_id(last_visitor_message) or (
            None if is_fragment else _first_non_empty(payload.get("messageId"), payload.get("message_id"), payload.get("id"))
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
