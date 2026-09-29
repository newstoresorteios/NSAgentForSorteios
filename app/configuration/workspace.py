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
    """Persist ownership and immediately expose the message in the Central."""
    if inbound_id is None or not workspace_id:
        return
    from app.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE public.ai_inbound_messages SET workspace_id=%s::uuid
                WHERE id=%s AND workspace_id IS NULL""", (workspace_id, inbound_id))
        conn.commit()
    try:
        from app.ops.central_conversation_sync import sync_agent_conversation

        sync_agent_conversation(inbound_id, workspace_id)
    except Exception as exc:  # Projection must never prevent the agent reply.
        from app.ops.observability import log_exception

        log_exception(
            "central.inbound_sync_failed",
            exc,
            {"inbound_id": inbound_id, "workspace_id": workspace_id},
        )


def ingress_settings(incoming, base):
    """Read published grouping controls before choosing direct vs queued ingress."""
    if not getattr(base, 'database_url', None) or not getattr(base, 'agent_db_persona_enabled', False):
        return base
    from app.persona.persona_runtime import load_persona_runtime
    from app.configuration.runtime import settings_from_bundle
    workspace = resolve_conversation_workspace(incoming.conversation_id, incoming.channel)
    persona = load_persona_runtime(workspace_id=workspace) if workspace else load_persona_runtime()
    if not persona.configuration_bundle:
        raise RuntimeError('ingress_configuration_unavailable')
    return settings_from_bundle(base, persona.configuration_bundle)


def stamp_silent_inbound_workspace(incoming, inbound_id: int | None) -> None:
    """Resolve ownership even when the agent pipeline is intentionally skipped."""
    from app.config import get_settings
    if not getattr(get_settings(), "database_url", None):
        return
    if inbound_id is None:
        raise ValueError("silent_inbound_id_missing")
    workspace = resolve_conversation_workspace(incoming.conversation_id, incoming.channel)
    if not workspace:
        from app.persona.persona_runtime import load_persona_runtime
        workspace = load_persona_runtime().flow_params_dict().get("workspace_id")
    if not workspace:
        raise ValueError("silent_inbound_workspace_unresolved")
    stamp_inbound_workspace(inbound_id, workspace)
