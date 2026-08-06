"""Inbox worker: process leased Brevo/Meta inbound rows (FASE 2)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.ingress.inbox import (
    claim_pending_inbox,
    mark_inbox_failed,
    mark_inbox_processed,
)
from app.observability import log_event, log_exception


async def process_inbox_batch(
    *,
    limit: int | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any]:
    """Claim and process pending inbox rows.

    Full agent turn for Brevo is still handled by the sync webhook path until
    `AGENT_ASYNC_INGRESS_ENABLED` is flipped and the thin enqueue path is live.
    This worker marks rows and is the hook Meta/Brevo async will call.
    """
    settings = get_settings()
    batch = int(limit or getattr(settings, "agent_inbox_batch_size", 5) or 5)
    lease = int(
        lease_seconds or getattr(settings, "agent_inbox_lease_seconds", 120) or 120
    )
    rows = claim_pending_inbox(limit=batch, lease_seconds=lease)
    processed = 0
    failed = 0
    for row in rows:
        inbox_id = int(row["id"])
        try:
            # Placeholder processor: durable claim/complete path.
            # FASE 3 wires Meta + Brevo thin enqueue to re-enter the agent here.
            mark_inbox_processed(inbox_id)
            processed += 1
            log_event(
                "inbox.worker_noop_processed",
                {
                    "inbox_id": inbox_id,
                    "provider": row.get("provider"),
                    "channel": row.get("channel"),
                    "async_enabled": bool(
                        getattr(settings, "agent_async_ingress_enabled", False)
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            attempts = int(row.get("attempts") or 1)
            max_attempts = 8
            log_exception(
                "inbox.worker_item_failed",
                exc,
                {"inbox_id": inbox_id},
            )
            mark_inbox_failed(
                inbox_id,
                error=f"{type(exc).__name__}",
                dead=attempts >= max_attempts,
            )
    return {
        "ok": True,
        "claimed": len(rows),
        "processed": processed,
        "failed": failed,
    }
