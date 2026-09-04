from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.models import BrevoSendResult, AgentResult, IncomingMessage
from app.ops.observability import log_event, record_brevo_send
from app.identity.repository import normalize_phone

BREVO_WHATSAPP_SEND_URL = "https://api.brevo.com/v3/whatsapp/sendMessage"
BREVO_CONVERSATIONS_SEND_URL = "https://api.brevo.com/v3/conversations/messages"
_BREVO_API_ROOTS = {
    "https://api.brevo.com",
    "http://api.brevo.com",
    "https://api.brevo.com/v3",
    "http://api.brevo.com/v3",
}


def resolved_brevo_whatsapp_send_url(raw: str | None) -> tuple[str, str | None]:
    """Ignore truncated BREVO_SEND_URL values like https://api.brevo.com/v3."""
    value = (raw or "").strip()
    if not value:
        return BREVO_WHATSAPP_SEND_URL, None
    folded = value.rstrip("/").casefold()
    if folded in {root.casefold() for root in _BREVO_API_ROOTS}:
        return BREVO_WHATSAPP_SEND_URL, "truncated_api_root"
    if "api.brevo.com" in folded and "/whatsapp/" not in folded:
        return BREVO_WHATSAPP_SEND_URL, "missing_whatsapp_path"
    return value, None

# Never send this caption to the customer — WhatsApp shows it as a failed PTT.
_AUDIO_CAPTION_PLACEHOLDER = "Resposta em áudio"
_EMPTY_REPLY_FALLBACK = (
    "Não consegui montar a resposta agora. Me diz em uma frase o que você busca "
    "(marca, modelo ou faixa de investimento)?"
)


def customer_visible_reply_text(
    text: str | None,
    *,
    audio_url: str | None = None,
) -> str:
    """Strip media placeholders; empty outbound must become useful copy."""
    cleaned = (text or "").strip()
    if cleaned.casefold() == _AUDIO_CAPTION_PLACEHOLDER.casefold():
        cleaned = ""
    if audio_url and not cleaned:
        return f"Ouça: {audio_url}"
    if cleaned:
        return cleaned
    return _EMPTY_REPLY_FALLBACK


def _agent_payload(settings: Any) -> dict[str, str]:
    if settings.brevo_agent_id:
        return {"agentId": settings.brevo_agent_id}

    if settings.brevo_agent_email and settings.brevo_agent_name:
        payload = {
            "agentEmail": settings.brevo_agent_email,
            "agentName": settings.brevo_agent_name,
            "receivedFrom": settings.brevo_received_from or settings.brevo_agent_name,
        }
        return payload

    return {}


def _build_brevo_audio_file(
    url: str,
    size: int,
    filename: str = "resposta.ogg",
    mime_type: str = "audio/ogg; codecs=opus",
) -> dict[str, Any]:
    return {
        "name": filename,
        "link": url,
        "mimeType": mime_type,
        "size": max(size, 1),
    }


async def _send_conversations_reply(
    incoming: IncomingMessage,
    text: str,
    audio_file: dict[str, Any] | None = None,
) -> BrevoSendResult:
    settings = get_settings()

    if not settings.brevo_api_key:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_api_key_missing")

    if not incoming.visitor_id:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_visitor_id_missing")

    agent_payload = _agent_payload(settings)
    if not agent_payload:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_agent_not_configured")

    payload: dict[str, Any] = {
        "text": customer_visible_reply_text(text),
        "visitorId": incoming.visitor_id,
        **agent_payload,
    }
    if audio_file:
        payload["file"] = audio_file

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": settings.brevo_api_key,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(BREVO_CONVERSATIONS_SEND_URL, json=payload, headers=headers)
        try:
            body = resp.json()
        except Exception as exc:
            from app.channels import log_swallowed

            log_swallowed("brevo.conversations_json", exc)
            body = {"text": resp.text[:500]}

    ok = 200 <= resp.status_code < 300
    if not ok:
        log_event(
            "brevo.send.conversations_failed",
            {
                "status_code": resp.status_code,
                "visitor_id_present": bool(incoming.visitor_id),
                "channel": incoming.channel,
            },
        )

    return BrevoSendResult(
        ok=ok,
        dry_run=False,
        status_code=resp.status_code,
        provider_response=body,
        error=None if ok else "brevo_conversations_send_failed",
    )


async def _send_whatsapp_transactional_reply(incoming: IncomingMessage, text: str) -> BrevoSendResult:
    settings = get_settings()

    if not settings.brevo_api_key:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_api_key_missing")

    if not settings.brevo_sender_number:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_sender_number_missing")

    recipient = normalize_phone(incoming.sender_phone)
    sender = normalize_phone(settings.brevo_sender_number)
    if not recipient:
        return BrevoSendResult(ok=False, dry_run=False, error="recipient_phone_missing")
    if not sender:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_sender_number_invalid")

    send_url, rewrite_reason = resolved_brevo_whatsapp_send_url(settings.brevo_send_url)
    if rewrite_reason:
        log_event(
            "brevo.send.url_rewritten",
            {"reason": rewrite_reason},
        )

    payload: dict[str, Any] = {
        "contactNumbers": [recipient],
        "senderNumber": sender,
        "text": text,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": settings.brevo_api_key,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(send_url, json=payload, headers=headers)
        try:
            body = resp.json()
        except Exception as exc:
            from app.channels import log_swallowed

            log_swallowed("brevo.whatsapp_json", exc)
            body = {"text": resp.text[:500]}

    ok = 200 <= resp.status_code < 300
    if not ok:
        log_event(
            "brevo.send.whatsapp_failed",
            {
                "status_code": resp.status_code,
                "recipient_present": bool(recipient),
                "sender_present": bool(sender),
                "channel": incoming.channel,
            },
        )

    return BrevoSendResult(
        ok=ok,
        dry_run=False,
        status_code=resp.status_code,
        provider_response=body,
        error=None if ok else "brevo_send_failed",
    )


async def send_brevo_reply(incoming: IncomingMessage, result: AgentResult | str) -> BrevoSendResult:
    """Send a reply back to the user through Brevo."""
    settings = get_settings()
    text = result.reply_text if isinstance(result, AgentResult) else str(result)
    has_outbound_image = bool(
        isinstance(result, AgentResult)
        and result.response_metadata.get("outbound_image_url")
    )
    audio_file: dict[str, Any] | None = None
    mode = (settings.brevo_reply_mode or "dry_run").lower()
    audio_url = (
        result.reply_audio_url
        if isinstance(result, AgentResult)
        else None
    )

    if (
        isinstance(result, AgentResult)
        and result.reply_modality == "audio"
        and result.reply_audio_url
        and settings.brevo_send_audio_as_attachment
    ):
        audio_file = _build_brevo_audio_file(
            url=result.reply_audio_url,
            size=len(result.reply_audio_bytes or b""),
            filename="resposta.ogg" if result.reply_audio_url.endswith(".ogg") else "resposta.mp3",
            mime_type=result.reply_audio_mime_type or "audio/ogg; codecs=opus",
        )
    elif isinstance(result, AgentResult) and result.reply_audio_url and not settings.brevo_send_audio_as_attachment:
        text = f"{text}\n\nOuça: {result.reply_audio_url}".strip()
    text = customer_visible_reply_text(text, audio_url=audio_url)

    if isinstance(result, AgentResult) and result.reply_modality == "audio" and not result.reply_audio_url:
        log_event(
            "brevo.send.audio_reply_fallback_to_text",
            {
                "reason": "supabase_upload_or_tts_failed",
                "audio_bytes": len(result.reply_audio_bytes or b""),
            },
        )

    if incoming.channel in {"instagram", "facebook", "widget"} and audio_file:
        audio_file = None

    if settings.dry_run or mode == "dry_run":
        sent = BrevoSendResult(
            ok=True,
            dry_run=True,
            provider_response={
                "mode": mode,
                "to": incoming.sender_phone,
                "visitor_id": incoming.visitor_id,
                "text": text,
                "reply_modality": result.reply_modality if isinstance(result, AgentResult) else "text",
                "audio_file": audio_file,
            },
        )
        channel = "dry_run"
    elif incoming.channel == "whatsapp" and incoming.sender_phone:
        # Prefer transactional WhatsApp API for phone delivery. Conversations
        # "send as agent" can return HTTP 200 while the message stays only in
        # the Brevo inbox (not pushed to the customer's WhatsApp).
        # Ignore BREVO_REPLY_MODE=conversations for WhatsApp+phone.
        if (
            audio_file
            and isinstance(result, AgentResult)
            and result.reply_audio_url
            and result.reply_audio_url not in text
        ):
            text = f"{text}\n\nOuça: {result.reply_audio_url}".strip()
        tx = await _send_whatsapp_transactional_reply(incoming, text)
        if tx.ok:
            sent = BrevoSendResult(
                ok=True,
                dry_run=False,
                status_code=tx.status_code,
                provider_response={
                    "route": "whatsapp_transactional",
                    "transactional": tx.provider_response,
                },
                error=None,
            )
            channel = "whatsapp"
        elif incoming.visitor_id:
            log_event(
                "brevo.send.whatsapp_transactional_fallback_conversations",
                {
                    "transactional_error": tx.error,
                    "transactional_status": tx.status_code,
                    "visitor_id_present": True,
                },
            )
            conv = await _send_conversations_reply(incoming, text, audio_file=None)
            sent = BrevoSendResult(
                ok=bool(conv.ok),
                dry_run=False,
                status_code=conv.status_code,
                provider_response={
                    "route": "brevo_conversations_fallback",
                    "transactional_error": tx.error,
                    "transactional_status": tx.status_code,
                    "transactional": tx.provider_response,
                    "conversations": conv.provider_response,
                },
                error=None if conv.ok else (conv.error or tx.error),
            )
            channel = "brevo_conversations_fallback"
        else:
            sent = BrevoSendResult(
                ok=False,
                dry_run=False,
                status_code=tx.status_code,
                provider_response={
                    "route": "whatsapp_transactional",
                    "transactional": tx.provider_response,
                },
                error=tx.error or "brevo_send_failed",
            )
            channel = "whatsapp"
    elif incoming.channel in {"instagram", "facebook", "widget"} and incoming.visitor_id:
        sent = await _send_conversations_reply(incoming, text, audio_file=audio_file)
        channel = "brevo_conversations"
    elif incoming.channel in {"instagram", "facebook", "widget"}:
        sent = BrevoSendResult(
            ok=False,
            dry_run=False,
            error="brevo_recipient_missing",
        )
        channel = "none"
    elif incoming.visitor_id:
        sent = await _send_conversations_reply(incoming, text, audio_file=audio_file)
        channel = "brevo_conversations"
    elif incoming.sender_phone:
        sent = await _send_whatsapp_transactional_reply(incoming, text)
        channel = "whatsapp"
    else:
        sent = BrevoSendResult(
            ok=False,
            dry_run=False,
            error="brevo_recipient_missing",
        )
        channel = "none"
    if has_outbound_image:
        media_send_supported = False
        media_send_failed = False
        fallback_link_sent = bool(sent.ok and not sent.dry_run)
        fallback_link_failed = bool(not sent.ok and not sent.dry_run)
        if isinstance(result, AgentResult):
            result.response_metadata.update({
                "image_url_found": True,
                "media_send_supported": media_send_supported,
                "media_send_failed": media_send_failed,
                "fallback_link_sent": fallback_link_sent,
                "fallback_link_failed": fallback_link_failed,
            })
        log_event(
            "sales.media.send",
            {
                "channel": channel,
                "image_url_found": True,
                "media_send_supported": media_send_supported,
                "media_send_failed": media_send_failed,
                "fallback_link_sent": fallback_link_sent,
                "fallback_link_failed": fallback_link_failed,
            },
        )
    record_brevo_send(
        channel=channel,
        ok=bool(sent.ok),
        dry_run=bool(sent.dry_run),
        status_code=sent.status_code,
        error=sent.error,
        reply_preview=text,
        reply_modality=(
            result.reply_modality if isinstance(result, AgentResult) else "text"
        ),
        visitor_id_present=bool(incoming.visitor_id),
        sender_phone_present=bool(incoming.sender_phone),
        extra={
            "inbound_channel": incoming.channel,
            "has_outbound_image": has_outbound_image,
            "has_audio_file": bool(audio_file),
        },
    )
    return sent
