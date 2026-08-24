"""Coloca conversas na fila humana quando o agente faz handoff."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import get_conn
from app.models import IncomingMessage
from app.observability import log_event

logger = logging.getLogger(__name__)


def _candidate_keys(incoming: IncomingMessage) -> list[str]:
    keys: list[str] = []
    for value in (
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
        incoming.visitor_id,
        incoming.source_conversation_ref,
    ):
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def mark_conversa_for_human_handoff(
    incoming: IncomingMessage,
    *,
    reason: str,
) -> list[str]:
    """Marca conversas abertas como `waiting` e libera assignee para a Central."""
    keys = _candidate_keys(incoming)
    if not keys:
        return []

    now = datetime.now(timezone.utc)
    updated_ids: list[str] = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'conversas'
                    LIMIT 1
                    """
                )
                if cur.fetchone() is None:
                    return []

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'conversas'
                    """
                )
                available = {
                    str(row["column_name"])
                    for row in (cur.fetchall() or [])
                    if row.get("column_name")
                }
                if "status" not in available:
                    return []

                set_parts = ["status = %s"]
                params: list[Any] = ["waiting"]

                if "bot_activated" in available:
                    set_parts.append("bot_activated = %s")
                    params.append(False)
                if "assigned_to" in available:
                    set_parts.append("assigned_to = NULL")
                if "updated_at" in available:
                    set_parts.append("updated_at = %s")
                    params.append(now)

                where_parts = ["status IS DISTINCT FROM 'closed'"]
                match_parts: list[str] = []
                if "external_thread_id" in available:
                    match_parts.append("external_thread_id = ANY(%s)")
                if "contact_phone" in available:
                    match_parts.append("contact_phone = ANY(%s)")
                if not match_parts:
                    return []

                where_parts.append(f"({' OR '.join(match_parts)})")
                for _ in match_parts:
                    params.append(keys)

                sql = f"""
                    UPDATE public.conversas
                    SET {", ".join(set_parts)}
                    WHERE {" AND ".join(where_parts)}
                    RETURNING id
                """
                cur.execute(sql, params)
                for row in cur.fetchall() or []:
                    conv_id = str(row.get("id") or "").strip()
                    if conv_id:
                        updated_ids.append(conv_id)
            conn.commit()
    except Exception as exc:
        logger.warning("Falha ao marcar conversa para handoff: %s", exc)
        return []

    if updated_ids:
        log_event(
            "handoff.queue.marked",
            {
                "reason": reason,
                "conversation_ids": updated_ids,
                "keys": keys[:4],
            },
        )
    return updated_ids
