from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings
from .models import BrevoSendResult
from .repository import normalize_phone

BREVO_WHATSAPP_SEND_URL = "https://api.brevo.com/v3/whatsapp/sendMessage"


async def send_whatsapp_reply(to_phone: str | None, text: str) -> BrevoSendResult:
    """Send a WhatsApp reply through Brevo's transactional API.

    Requires an active customer session (user already messaged you). Within that
    window, Brevo accepts plain `text` replies without a template.
    """
    settings = get_settings()
    mode = (settings.brevo_reply_mode or "dry_run").lower()

    if settings.dry_run or mode == "dry_run":
        return BrevoSendResult(ok=True, dry_run=True, provider_response={"to": to_phone, "text": text})

    if not settings.brevo_api_key:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_api_key_missing")

    if not settings.brevo_sender_number:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_sender_number_missing")

    recipient = normalize_phone(to_phone)
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
        print("[brevo.send] failed", {
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
