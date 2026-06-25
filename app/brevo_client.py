from __future__ import annotations
from typing import Any
import httpx
from .config import get_settings
from .models import BrevoSendResult


async def send_whatsapp_reply(to_phone: str | None, text: str) -> BrevoSendResult:
    """Send a reply through a configurable Brevo outbound endpoint.

    Default mode is dry_run so the webhook can be tested without sending messages.
    Configure BREVO_SEND_URL only after confirming the correct Brevo endpoint for
    the specific Brevo product connected to the WhatsApp account.
    """
    settings = get_settings()
    mode = (settings.brevo_reply_mode or "dry_run").lower()

    if settings.dry_run or mode == "dry_run":
        return BrevoSendResult(ok=True, dry_run=True, provider_response={"to": to_phone, "text": text})

    if not settings.brevo_api_key:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_api_key_missing")

    if not settings.brevo_send_url:
        return BrevoSendResult(ok=False, dry_run=False, error="brevo_send_url_missing")

    payload: dict[str, Any] = {
        "to": to_phone,
        "text": text,
    }
    if settings.brevo_sender_number:
        payload["senderNumber"] = settings.brevo_sender_number

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": settings.brevo_api_key,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(settings.brevo_send_url, json=payload, headers=headers)
        try:
            body = resp.json()
        except Exception:
            body = {"text": resp.text[:500]}

    return BrevoSendResult(
        ok=200 <= resp.status_code < 300,
        dry_run=False,
        status_code=resp.status_code,
        provider_response=body,
        error=None if 200 <= resp.status_code < 300 else "brevo_send_failed",
    )
