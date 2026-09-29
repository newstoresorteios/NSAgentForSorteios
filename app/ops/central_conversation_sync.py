"""Project the agent audit trail into the shared Conversion Center tables.

The AI tables remain the durable source of truth for agent execution.  This
module keeps the operator-facing conversation tables current in the same
transaction boundary in which ownership is already known.  Every write is
idempotent so it can safely coexist with the legacy/external projector.
"""
from __future__ import annotations

from typing import Any

from app.ops.observability import log_event


def sync_agent_conversation(inbound_id: int, workspace_id: str) -> dict[str, Any]:
    """Backfill every known AI message for one conversation into the Central."""
    from app.db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT conversation_id, channel,
                       COALESCE(NULLIF(sender_username, ''),
                                NULLIF(sender_name, ''),
                                NULLIF(sender_key, ''),
                                'Cliente') AS customer_name
                FROM public.ai_inbound_messages
                WHERE id = %(inbound_id)s
                  AND workspace_id = %(workspace_id)s::uuid
                """,
                {"inbound_id": inbound_id, "workspace_id": workspace_id},
            )
            source = cur.fetchone()
            if not source or not source.get("conversation_id"):
                return {"ok": False, "reason": "inbound_or_conversation_missing"}

            params = {
                "workspace_id": workspace_id,
                "conversation_id": str(source["conversation_id"]),
                "channel": str(source.get("channel") or "unknown"),
                "customer_name": str(source.get("customer_name") or "Cliente")[:160],
            }

            # Create only when ChatBo has not already created the thread.  The
            # partial unique index on active threads protects concurrent writers.
            cur.execute(
                """
                INSERT INTO public.conversas
                    (customer_name, channel, external_thread_id, workspace_id,
                     status, bot_activated)
                SELECT %(customer_name)s, %(channel)s, %(conversation_id)s,
                       %(workspace_id)s::uuid, 'active', true
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.conversas
                    WHERE workspace_id = %(workspace_id)s::uuid
                      AND channel = %(channel)s
                      AND external_thread_id = %(conversation_id)s
                      AND merged_into IS NULL
                )
                ON CONFLICT DO NOTHING
                """,
                params,
            )
            cur.execute(
                """
                SELECT id
                FROM public.conversas
                WHERE workspace_id = %(workspace_id)s::uuid
                  AND channel = %(channel)s
                  AND external_thread_id = %(conversation_id)s
                  AND merged_into IS NULL
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                params,
            )
            conversation = cur.fetchone()
            if not conversation:
                raise RuntimeError("central_conversation_unavailable")
            conversation_id = str(conversation["id"])
            sync_params = {**params, "central_conversation_id": conversation_id}

            # Reconcile the whole NSAgent history for the thread.  The global
            # external_id uniqueness makes retries and the external projector safe.
            cur.execute(
                """
                INSERT INTO public.mensagens
                    (conversa_id, content, sender, status, created_at, external_id,
                     direction, provider_status, workspace_id)
                SELECT %(central_conversation_id)s::uuid,
                       inbound.text,
                       'customer',
                       'delivered',
                       inbound.created_at,
                       'ai-in-' || inbound.id::text,
                       'inbound',
                       'received',
                       %(workspace_id)s::uuid
                FROM public.ai_inbound_messages AS inbound
                WHERE inbound.workspace_id = %(workspace_id)s::uuid
                  AND inbound.conversation_id = %(conversation_id)s
                  AND inbound.channel = %(channel)s
                  AND NULLIF(inbound.text, '') IS NOT NULL
                ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO NOTHING
                """,
                sync_params,
            )
            inbound_inserted = max(cur.rowcount, 0)

            cur.execute(
                """
                INSERT INTO public.mensagens
                    (conversa_id, content, sender, status, created_at, external_id,
                     direction, provider_status, workspace_id)
                SELECT %(central_conversation_id)s::uuid,
                       response.reply_text,
                       'ai',
                       'sent',
                       response.created_at,
                       'ai-out-' || response.id::text,
                       'outbound',
                       'sent',
                       %(workspace_id)s::uuid
                FROM public.ai_agent_responses AS response
                JOIN public.ai_inbound_messages AS inbound
                  ON inbound.id = response.inbound_id
                WHERE inbound.workspace_id = %(workspace_id)s::uuid
                  AND inbound.conversation_id = %(conversation_id)s
                  AND inbound.channel = %(channel)s
                  AND response.workspace_id = %(workspace_id)s::uuid
                  AND response.provider_send_ok = true
                  AND NULLIF(response.reply_text, '') IS NOT NULL
                ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO NOTHING
                """,
                sync_params,
            )
            outbound_inserted = max(cur.rowcount, 0)

            # Derive the conversation preview from the actual projected rows so
            # older backfills never overwrite a newer operator-visible message.
            cur.execute(
                """
                WITH latest AS (
                    SELECT content, created_at
                    FROM public.mensagens
                    WHERE conversa_id = %(central_conversation_id)s::uuid
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                UPDATE public.conversas AS conversation
                SET last_message = latest.content,
                    last_message_at = latest.created_at,
                    updated_at = GREATEST(COALESCE(conversation.updated_at, latest.created_at),
                                          latest.created_at)
                FROM latest
                WHERE conversation.id = %(central_conversation_id)s::uuid
                  AND (conversation.last_message_at IS NULL
                       OR conversation.last_message_at <= latest.created_at)
                """,
                sync_params,
            )

    result = {
        "ok": True,
        "conversation_id": conversation_id,
        "inbound_inserted": inbound_inserted,
        "outbound_inserted": outbound_inserted,
    }
    log_event("central.sync", result)
    return result
