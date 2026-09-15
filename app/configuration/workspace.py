"""Resolve workspace ownership from server-side conversation records."""
from __future__ import annotations


def resolve_conversation_workspace(conversation_id: str | None, channel: str | None) -> str | None:
    from app.config import get_settings
    settings = get_settings()
    if not conversation_id or not getattr(settings, "database_url", None):
        return None
    from app.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT workspace_id FROM public.conversas
                WHERE workspace_id IS NOT NULL
                  AND (id::text=%s OR external_thread_id=%s)
                  AND (%s::text IS NULL OR channel=%s)
                LIMIT 2
            """, (conversation_id, conversation_id, channel, channel))
            rows = list(cur.fetchall())
    if len(rows) > 1:
        raise ValueError("ambiguous_conversation_workspace")
    return str(rows[0]["workspace_id"]) if rows else None


def stamp_inbound_workspace(inbound_id: int | None, workspace_id: str | None) -> None:
    """Persist server-resolved ownership without ever reassigning another workspace."""
    if inbound_id is None or not workspace_id:
        return
    from app.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.ai_inbound_messages SET workspace_id=%s::uuid
                WHERE id=%s AND workspace_id IS NULL""", (workspace_id, inbound_id))
        conn.commit()
