"""Drain outbound outbox retries (send failures from inbox worker)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.ingress.outbox import (
    claim_pending_outbox,
    mark_outbox_failed,
    mark_outbox_sent,
)
from app.ops.observability import log_event, log_exception


async def _resend_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    provider = str(row.get("provider") or "").lower()
    channel = str(row.get("channel") or "").lower()
    reply_text = str(row.get("reply_text") or "")
    if not reply_text.strip():
        return {"ok": False, "error": "empty_reply"}

    from app.ingress.outbox import incoming_from_outbox_row
    from app.ingress.worker import _send_reply
    from app.models import AgentResult

    incoming = incoming_from_outbox_row(row)
    if not incoming.provider:
        incoming.provider = provider or "brevo"
    if not incoming.channel:
        incoming.channel = channel or "whatsapp"
    result = AgentResult(
        reply_text=reply_text,
        intent="commerce",
        handoff_required=False,
    )
    return await _send_reply(incoming, result)


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
