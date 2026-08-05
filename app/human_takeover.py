"""Detecta quando a Central ChatBô assumiu a conversa (humano no comando)."""

from __future__ import annotations

import logging
from typing import Any

from app.db import get_conn
from app.models import IncomingMessage

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


def human_takeover_active(incoming: IncomingMessage) -> bool:
    """True se `conversas` (ChatBô) tem atendente atribuído ou bot pausado.

    Mesmo Postgres/Supabase do Chatbo-backendAgent. Se a tabela não existir
    ou a consulta falhar, retorna False (agente segue normalmente).
    """
    keys = _candidate_keys(incoming)
    if not keys:
        return False

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Confirma existência da tabela do ChatBô no mesmo DB.
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'conversas'
                    LIMIT 1
                    """
                )
                if cur.fetchone() is None:
                    return False

                cur.execute(
                    """
                    SELECT assigned_to, bot_activated, status
                    FROM public.conversas
                    WHERE
                        external_thread_id = ANY(%s)
                        OR contact_phone = ANY(%s)
                    ORDER BY last_message_at DESC NULLS LAST
                    LIMIT 8
                    """,
                    (keys, keys),
                )
                rows: list[dict[str, Any]] = list(cur.fetchall() or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("human_takeover lookup failed: %s", exc)
        return False

    for row in rows:
        if row.get("status") == "closed":
            continue
        assigned = row.get("assigned_to")
        if assigned is not None and str(assigned).strip():
            logger.info(
                "human_takeover active assigned_to=%s keys=%s",
                str(assigned)[:36],
                keys[:3],
            )
            return True
        if row.get("bot_activated") is False:
            logger.info("human_takeover active bot_activated=false keys=%s", keys[:3])
            return True
    return False
