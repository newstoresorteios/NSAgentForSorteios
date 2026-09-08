"""Defer an inbound when the conversation lock is busy.

Do not raise the Hobby 15s lock. After timeout, persist the same inbound and
ack 200 so Brevo does not retry and change the meaning of the turn.
"""

from __future__ import annotations

from typing import Any

from app.models import IncomingMessage
from app.ops.observability import log_event, log_exception

LOCK_DEFERRED_RETRY_SECONDS = 2.0
LOCK_DEFERRED_DRAIN_LIMIT = 3


def inbox_drain_keys(
    *,
    lock_key: str | None,
    incoming: IncomingMessage,
) -> list[str]:
    keys: list[str] = []
    for value in (
        lock_key,
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
    ):
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def enqueue_lock_deferred_inbound(
    incoming: IncomingMessage,
    *,
    event_name: str | None,
    conversation_key: str,
) -> tuple[bool, int | None]:
    from app.ingress.inbox import enqueue_inbound

    created, inbox_id = enqueue_inbound(
        provider=incoming.provider or "brevo",
        channel=incoming.channel,
        message_id=incoming.message_id,
        conversation_key=conversation_key,
        visitor_id=incoming.visitor_id,
        sender_key=incoming.sender_key,
        event_name=event_name,
        payload={
            "normalized": incoming.model_dump(mode="json"),
            "raw": incoming.raw if isinstance(incoming.raw, dict) else {},
        },
    )
    log_event(
        "agent.lock.deferred",
        {
            "created": created,
            "inbox_id": inbox_id,
            "channel": incoming.channel,
            "event_name": event_name,
        },
    )
    return created, inbox_id


async def drain_lock_deferred_inbound(
    *,
    conversation_keys: list[str] | None,
    database_url: str = "",
    limit: int = LOCK_DEFERRED_DRAIN_LIMIT,
) -> dict[str, Any]:
    keys = [str(key).strip() for key in (conversation_keys or []) if str(key or "").strip()]
    if not keys:
        return {"ok": True, "claimed": 0}
    from app.ops.conversation_lock import (
        ConversationLockUnavailable,
        acquire_conversation_lock,
        release_conversation_lock,
    )

    try:
        handle = await acquire_conversation_lock(
            keys[0],
            database_url=database_url,
            timeout_seconds=LOCK_DEFERRED_RETRY_SECONDS,
        )
    except ConversationLockUnavailable:
        return {"ok": True, "claimed": 0, "deferred": True}

    processed = 0
    failed = 0
    try:
        from app.ingress.inbox import claim_pending_inbox
        from app.ingress.worker import process_inbox_row

        rows = claim_pending_inbox(
            limit=max(1, min(int(limit), 5)),
            conversation_keys=keys,
        )
        for row in rows:
            try:
                item = await process_inbox_row(row, lock_held=True)
                if item.get("ok"):
                    processed += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                log_exception(
                    "agent.lock.deferred_drain_item_failed",
                    exc,
                    {"inbox_id": row.get("id")},
                )
        return {
            "ok": True,
            "claimed": len(rows),
            "processed": processed,
            "failed": failed,
        }
    finally:
        await release_conversation_lock(handle)
