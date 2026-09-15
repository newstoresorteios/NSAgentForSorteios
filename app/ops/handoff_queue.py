"""Transfer only the server-resolved conversation to human attendance."""
from __future__ import annotations

import logging
import json
from datetime import datetime, timezone

from app.db import get_conn, to_jsonb
from app.models import IncomingMessage
from app.ops.observability import log_event

logger = logging.getLogger(__name__)


def mark_conversa_for_human_handoff(incoming: IncomingMessage, *, reason: str) -> list[str]:
    # Phone numbers identify contacts, not conversations or ownership.
    thread = str(incoming.conversation_id or "").strip()
    channel = str(incoming.channel or "").strip()
    inbound_id = (incoming.raw or {}).get("inbound_id")
    if not thread or channel in {"", "unknown"} or inbound_id is None:
        return []
    updated_ids: list[str] = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT workspace_id FROM public.ai_inbound_messages
                    WHERE id=%s AND conversation_id=%s AND channel=%s
                """, (inbound_id, thread, channel))
                inbound = cur.fetchone()
                if not inbound or not inbound.get("workspace_id"):
                    log_event("handoff.queue.skipped", {"reason": "inbound_owner_unresolved"})
                    return []
                workspace_id = str(inbound["workspace_id"])
                cur.execute("""
                    SELECT DISTINCT target.id, target.workspace_id, target.channel
                    FROM public.conversas source
                    JOIN public.conversas target
                      ON target.id=coalesce(source.merged_into,source.id)
                     AND target.workspace_id=source.workspace_id
                     AND target.channel=source.channel
                     AND coalesce(target.canal_id,'')=coalesce(source.canal_id,'')
                    WHERE (source.id::text=%s OR source.external_thread_id=%s)
                      AND source.channel=%s AND source.workspace_id IS NOT NULL
                      AND source.workspace_id=%s::uuid
                      AND target.merged_into IS NULL
                    LIMIT 2
                """, (thread, thread, channel, workspace_id))
                targets = list(cur.fetchall())
                if len(targets) != 1:
                    log_event("handoff.queue.skipped", {"reason": "conversation_not_unique", "matches": len(targets)})
                    return []
                target = targets[0]
                workspace_id = str(target["workspace_id"])
                scope = (str(target["id"]), workspace_id, channel)
                cur.execute("""
                    SELECT * FROM public.conversas
                    WHERE id=%s::uuid AND workspace_id=%s::uuid AND channel=%s
                      AND merged_into IS NULL FOR UPDATE
                """, scope)
                before = cur.fetchone()
                if not before or before["status"] == "closed":
                    return []
                if before.get("bot_activated") is False and before.get("status") == "waiting":
                    return [str(before["id"])]
                snapshot = dict(before)
                snapshot["_handoff"] = {"reason": reason, "inbound_id": inbound_id}
                cur.execute("""
                    INSERT INTO public.conversation_reconciliation_audit
                        (workspace_id,entity_type,entity_id,destination_id,original_row)
                    VALUES (%s::uuid,'conversation',%s::uuid,%s::uuid,%s)
                """, (workspace_id, scope[0], scope[0], to_jsonb(json.loads(json.dumps(snapshot, default=str)))))
                # Keep an existing operator assignment; it is not ours to erase.
                cur.execute("""
                    UPDATE public.conversas SET
                        status=CASE WHEN nullif(assigned_to,'') IS NULL THEN 'waiting' ELSE status END,
                        bot_activated=false, updated_at=%s
                    WHERE id=%s::uuid AND workspace_id=%s::uuid AND channel=%s
                      AND merged_into IS NULL AND status IS DISTINCT FROM 'closed'
                    RETURNING id
                """, (datetime.now(timezone.utc), *scope))
                updated_ids = [str(row["id"]) for row in cur.fetchall()]
            conn.commit()
    except Exception as exc:
        logger.warning("Falha ao marcar conversa para handoff: %s", type(exc).__name__)
        log_event("handoff.queue.failed", {"error_type": type(exc).__name__})
        return []
    if updated_ids:
        log_event("handoff.queue.marked", {"reason": reason, "conversation_ids": updated_ids, "workspace_id": workspace_id})
    return updated_ids
