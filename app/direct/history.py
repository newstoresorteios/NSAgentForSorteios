"""Only delivered, workspace-scoped history can authorize conversation reuse.

We reuse the existing response audit instead of creating a second session DB.
Remote conversation tails are checked before reuse: a failed/unrecorded send or
interrupted tool loop must never become something the customer supposedly saw.
"""
from __future__ import annotations

from app.db import get_conn


def load_history(incoming, workspace_id: str, *, limit: int = 30):
    identity = incoming.sender_key or incoming.sender_phone or incoming.visitor_id
    if not identity or not incoming.conversation_id:
        return [], {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.text, r.reply_text,
                       r.provider_response->'_agent_metadata' AS metadata
                FROM public.ai_inbound_messages i
                JOIN LATERAL (
                  SELECT reply_text, provider_response FROM public.ai_agent_responses
                  WHERE inbound_id=i.id AND workspace_id=%(workspace)s::uuid
                    AND provider_send_ok=true ORDER BY id DESC LIMIT 1
                ) r ON true
                WHERE i.workspace_id=%(workspace)s::uuid
                  AND (i.conversation_id=%(conversation)s OR (
                    %(brevo_continuity)s AND i.visitor_id=%(visitor)s
                    AND i.created_at >= now() - interval '24 hours'))
                  AND i.channel=%(channel)s
                  AND i.provider=%(provider)s
                  AND COALESCE(i.sender_key, i.sender_phone, i.visitor_id)=%(identity)s
                  AND (%(before)s::bigint IS NULL OR i.id < %(before)s::bigint)
                ORDER BY i.id DESC LIMIT %(limit)s
            """, {"workspace": workspace_id, "conversation": incoming.conversation_id,
                  "channel": incoming.channel, "provider": incoming.provider,
                  "identity": identity, "before": incoming.raw.get("inbound_id"), "limit": limit,
                  "brevo_continuity": bool(incoming.provider == 'brevo' and incoming.channel == 'whatsapp'
                                           and incoming.sender_key and incoming.visitor_id),
                  "visitor": incoming.visitor_id})
            rows = list(reversed(cur.fetchall()))
    history = []
    for row in rows:
        history.extend([{"role": "user", "content": row["text"] or "[Mídia recebida]"},
                        {"role": "assistant", "content": row["reply_text"],
                         "metadata": row.get("metadata") or {}}])
    return history, (rows[-1].get("metadata") or {}) if rows else {}
