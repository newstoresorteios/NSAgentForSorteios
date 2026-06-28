from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings
from .models import BrevoSendResult, AgentResult, IncomingMessage
from .repository import normalize_phone

BREVO_WHATSAPP_SEND_URL = "https://api.brevo.com/v3/whatsapp/sendMessage"
BREVO_CONVERSATIONS_SEND_URL = "https://api.brevo.com/v3/conversations/messages"


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
        "text": text or "Resposta em áudio",
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
        except Exception:
            body = {"text": resp.text[:500]}

    ok = 200 <= resp.status_code < 300
    if not ok:
        print("[brevo.send] conversations_failed", {
            "status_code": resp.status_code,
            "visitor_id_present": bool(incoming.visitor_id),
            "response_preview": str(body)[:300],
        })

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

    send_url = (settings.brevo_send_url or BREVO_WHATSAPP_SEND_URL).strip()

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
        except Exception:
            body = {"text": resp.text[:500]}

    ok = 200 <= resp.status_code < 300
    if not ok:
        print("[brevo.send] whatsapp_failed", {
            "status_code": resp.status_code,
            "recipient_present": bool(recipient),
            "sender_present": bool(sender),
            "response_preview": str(body)[:300],
        })

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
    audio_file: dict[str, Any] | None = None
    mode = (settings.brevo_reply_mode or "dry_run").lower()

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
        if not text.strip():
            text = "Resposta em áudio"
    elif isinstance(result, AgentResult) and result.reply_audio_url and not settings.brevo_send_audio_as_attachment:
        text = f"{text}\n\nOuça: {result.reply_audio_url}".strip()

    if isinstance(result, AgentResult) and result.reply_modality == "audio" and not result.reply_audio_url:
        print("[brevo.send] audio_reply_fallback_to_text", {
            "reason": "supabase_upload_or_tts_failed",
            "audio_bytes": len(result.reply_audio_bytes or b""),
        })

    if settings.dry_run or mode == "dry_run":
        return BrevoSendResult(
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

    if mode == "whatsapp":
        return await _send_whatsapp_transactional_reply(incoming, text)

    # Default live mode for Brevo Conversations inbound webhooks.
    if incoming.visitor_id:
        return await _send_conversations_reply(incoming, text, audio_file=audio_file)

    if incoming.sender_phone:
        return await _send_whatsapp_transactional_reply(incoming, text)

    return BrevoSendResult(ok=False, dry_run=False, error="brevo_recipient_missing")
