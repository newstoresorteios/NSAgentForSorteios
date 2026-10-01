"""Lightweight contact-memory consolidation (expire / prune)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import get_conn
from app.core.turn_cache import invalidates_turn_reads
from app.memory.workspace_scope import memory_workspace


@invalidates_turn_reads
def expire_contact_memories(*, tenant_id: str | None = None, workspace_id: str | None = None) -> int:
    """Mark expired active memories as expired. Returns affected rows."""
    workspace_id = memory_workspace(workspace_id, required=True)
    settings = get_settings()
    tenant = tenant_id or str(getattr(settings, "agent_persona_tenant_id", "newstore"))
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_contact_memories
                SET status = 'expired',
                    use_in_instructions = false,
                    updated_at = %s
                WHERE tenant_id = %s
                  AND workspace_id = %s::uuid
                  AND scope_status = 'verified'
                  AND status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at <= %s
                """,
                (now, tenant, workspace_id, now),
            )
            return int(cur.rowcount or 0)


@invalidates_turn_reads
def prune_excess_active_memories(
    *,
    tenant_id: str,
    sender_key: str,
    workspace_id: str | None = None,
    keep: int | None = None,
) -> int:
    """Keep the top-N active memories by importance; supersede the rest."""
    workspace_id = memory_workspace(workspace_id, required=True)
    settings = get_settings()
    limit = max(0, int(
        keep
        if keep is not None
        else getattr(settings, "agent_max_active_contact_memories", 20)
    ))
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM public.ai_contact_memories
                WHERE tenant_id = %s
                  AND workspace_id = %s::uuid
                  AND sender_key = %s
                  AND scope_status = 'verified'
                  AND status = 'active'
                ORDER BY importance DESC, last_confirmed_at DESC NULLS LAST, id DESC
                """,
                (tenant_id, workspace_id, sender_key),
            )
            rows = cur.fetchall() or []
            if len(rows) <= limit:
                return 0
            drop_ids = [int(row["id"]) for row in rows[limit:]]
            cur.execute(
                """
                UPDATE public.ai_contact_memories
                SET status = 'superseded',
                    use_in_instructions = false,
                    updated_at = %s
                WHERE id = ANY(%s)
                  AND tenant_id = %s
                  AND workspace_id = %s::uuid
                  AND sender_key = %s
                  AND scope_status = 'verified'
                  AND status = 'active'
                """,
                (now, drop_ids, tenant_id, workspace_id, sender_key),
            )
            return int(cur.rowcount or 0)


def consolidate_contact_memories(
    *,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    sender_key: str | None = None,
) -> dict[str, Any]:
    """Run safe consolidation steps. Never touches persona versions."""
    workspace_id = memory_workspace(workspace_id, required=True)
    settings = get_settings()
    tenant = tenant_id or str(getattr(settings, "agent_persona_tenant_id", "newstore"))
    expired = expire_contact_memories(tenant_id=tenant, workspace_id=workspace_id)
    pruned = 0
    if sender_key:
        pruned = prune_excess_active_memories(
            tenant_id=tenant,
            workspace_id=workspace_id,
            sender_key=sender_key,
        )
    return {
        "tenant_id": tenant,
        "workspace_id": workspace_id,
        "expired": expired,
        "pruned": pruned,
        "sender_key": sender_key,
    }
