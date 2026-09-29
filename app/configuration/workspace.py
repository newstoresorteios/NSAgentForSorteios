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


def resolve_ingress_workspace(
    conversation_id: str | None,
    channel: str | None,
    account_ref: str | None = None,
) -> str | None:
    """Resolve ownership for a new inbound without guessing between tenants.

    An existing Central conversation is the authoritative mapping.  A new
    Instagram/WhatsApp conversation has no such row yet, though, and loading a
    persona without a workspace is unsafe when more than one NSAgent persona
    is active.  In that case use the only workspace that is both active for
    NSAgent and connected to the Tray catalogue.  If that is not unique, leave
    it unresolved rather than sending a customer to an arbitrary tenant.
    """
    workspace = resolve_conversation_workspace(conversation_id, channel)
    if workspace:
        return workspace

    from app.config import get_settings
    if not getattr(get_settings(), "database_url", None):
        return None
    from app.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            if account_ref:
                cur.execute(
                    """
                    SELECT DISTINCT inbound.workspace_id
                    FROM public.ai_inbound_messages AS inbound
                    WHERE inbound.workspace_id IS NOT NULL
                      AND inbound.provider = 'meta'
                      AND inbound.channel = %(channel)s
                      AND (
                        inbound.raw->>'entry_id' = %(account_ref)s
                        OR inbound.raw#>>'{meta_event,recipient,id}' = %(account_ref)s
                      )
                    LIMIT 2
                    """,
                    {"channel": channel, "account_ref": account_ref},
                )
                account_rows = list(cur.fetchall())
                if len(account_rows) > 1:
                    raise ValueError("ambiguous_ingress_account_workspace")
                if account_rows:
                    return str(account_rows[0]["workspace_id"])
            cur.execute(
                """
                SELECT DISTINCT agent.workspace_id
                FROM public.workspace_agents AS agent
                JOIN public.workspace_integrations AS integration
                  ON integration.workspace_id = agent.workspace_id
                WHERE agent.agent_type = 'nsagent'
                  AND agent.status = 'active'
                  AND integration.provider = 'tray'
                  AND integration.status = 'connected'
                LIMIT 2
                """
            )
            rows = list(cur.fetchall())
    if len(rows) != 1:
        return None
    return str(rows[0]["workspace_id"])


def _incoming_account_ref(incoming) -> str | None:
    """Return the authenticated receiving account carried by the provider payload."""
    raw = incoming.raw if isinstance(getattr(incoming, "raw", None), dict) else {}
    event = raw.get("meta_event") if isinstance(raw.get("meta_event"), dict) else {}
    recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
    story = getattr(incoming, "instagram_story", None)
    candidates = (
        raw.get("entry_id"),
        recipient.get("id"),
        getattr(story, "instagram_account_id", None),
    )
    return next((str(value).strip() for value in candidates if str(value or "").strip()), None)


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
    """Resolve ownership before the agent pipeline or an intentional skip."""
    from app.config import get_settings
    if not getattr(get_settings(), "database_url", None):
        return
    if inbound_id is None:
        raise ValueError("silent_inbound_id_missing")
    workspace = resolve_ingress_workspace(
        incoming.conversation_id,
        incoming.channel,
        _incoming_account_ref(incoming),
    )
    if not workspace:
        raise ValueError("silent_inbound_workspace_unresolved")
    stamp_inbound_workspace(inbound_id, workspace)
