"""Drain outbound outbox retries (send failures from inbox worker)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.ingress.outbox import (
    claim_pending_outbox,
    mark_outbox_failed,
    mark_outbox_sent,
)
from app.observability import log_event, log_exception


async def _resend_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    provider = str(row.get("provider") or "").lower()
    channel = str(row.get("channel") or "").lower()
    reply_text = str(row.get("reply_text") or "")
    if not reply_text.strip():
        return {"ok": False, "error": "empty_reply"}

    # Minimal IncomingMessage-shaped send path.
    from app.models import AgentResult, IncomingMessage

    incoming = IncomingMessage(
        text="",
        channel=channel or "whatsapp",
        provider=provider or "brevo",
        conversation_id=row.get("conversation_key"),
        sender_key=row.get("sender_key"),
        visitor_id=row.get("visitor_id"),
        sender_external_id=row.get("recipient_external_id"),
    )
    result = AgentResult(
        reply_text=reply_text,
        intent="commerce",
        handoff_required=False,
    )

    if provider == "meta" or channel == "instagram":
        from app.channels.meta_instagram import send_meta_instagram_reply

        return await send_meta_instagram_reply(incoming, result)

    from app.brevo_client import send_brevo_reply

    send_result = await send_brevo_reply(incoming, result)
    return {
        "ok": bool(send_result.ok),
        "status_code": send_result.status_code,
        "provider_response": send_result.model_dump(),
        "error": send_result.error,
    }


async def process_outbox_batch(*, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    batch = int(limit or getattr(settings, "agent_inbox_batch_size", 5) or 5)
    rows = claim_pending_outbox(limit=batch)
    if not rows:
        return {"ok": True, "claimed": 0, "sent": 0, "failed": 0, "dead": 0}

    sent = failed = dead = 0
    details: list[dict[str, Any]] = []
    for row in rows:
        outbox_id = int(row["id"])
        attempts = int(row.get("attempts") or 1)
        try:
            send_info = await _resend_outbox_row(row)
        except Exception as exc:  # noqa: BLE001
            log_exception("outbox.resend_failed", exc, {"outbox_id": outbox_id})
            send_info = {"ok": False, "error": type(exc).__name__}

        if send_info.get("ok"):
            mark_outbox_sent(
                outbox_id,
                provider_response=send_info.get("provider_response")
                if isinstance(send_info.get("provider_response"), dict)
                else send_info,
            )
            sent += 1
            details.append({"id": outbox_id, "status": "sent"})
            continue

        max_attempts = 5
        is_dead = attempts >= max_attempts
        mark_outbox_failed(
            outbox_id,
            error=str(send_info.get("error") or "send_failed"),
            dead=is_dead,
        )
        if is_dead:
            dead += 1
            details.append({"id": outbox_id, "status": "dead"})
        else:
            failed += 1
            details.append({"id": outbox_id, "status": "failed"})

    summary = {
        "ok": True,
        "claimed": len(rows),
        "sent": sent,
        "failed": failed,
        "dead": dead,
        "items": details,
    }
    log_event("outbox.batch_processed", summary)
    return summary
