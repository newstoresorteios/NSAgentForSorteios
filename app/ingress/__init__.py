"""Ingress package: durable inbox + outbox for async webhooks."""

from __future__ import annotations


def log_swallowed(scope: str, exc: BaseException) -> None:
    """Keep optional ingress paths from failing the turn, but do not hide the type."""
    print(f"[ingress.{scope}]", {"error_type": type(exc).__name__})


from app.ingress.inbox import (
    build_idempotency_key,
    claim_pending_inbox,
    enqueue_inbound,
    mark_inbox_failed,
    mark_inbox_processed,
)
from app.ingress.outbox import (
    claim_pending_outbox,
    enqueue_outbound,
    mark_outbox_failed,
    mark_outbox_sent,
)

__all__ = [
    "log_swallowed",
    "build_idempotency_key",
    "claim_pending_inbox",
    "claim_pending_outbox",
    "enqueue_inbound",
    "enqueue_outbound",
    "mark_inbox_failed",
    "mark_inbox_processed",
    "mark_outbox_failed",
    "mark_outbox_sent",
]
