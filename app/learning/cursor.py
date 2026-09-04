"""Incremental attendance cursor: process every response since the last cutoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_conn, to_jsonb


def load_cursor(*, tenant_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, last_response_id, last_response_at, last_run_at, metadata
                FROM public.ai_learning_cursors
                WHERE tenant_id = %s
                LIMIT 1
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def save_cursor(
    *,
    tenant_id: str,
    last_response_id: int | None,
    last_response_at: datetime | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_learning_cursors (
                    tenant_id, last_response_id, last_response_at, last_run_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    last_response_id = COALESCE(
                        EXCLUDED.last_response_id,
                        public.ai_learning_cursors.last_response_id
                    ),
                    last_response_at = COALESCE(
                        EXCLUDED.last_response_at,
                        public.ai_learning_cursors.last_response_at
                    ),
                    last_run_at = EXCLUDED.last_run_at,
                    metadata = COALESCE(EXCLUDED.metadata, public.ai_learning_cursors.metadata)
                """,
                (
                    tenant_id,
                    last_response_id,
                    last_response_at,
                    now,
                    to_jsonb(metadata or {}),
                ),
            )


def fetch_attendances_since(
    *,
    tenant_id: str,
    last_response_id: int | None,
    limit: int,
    bootstrap_hours: int,
) -> list[dict[str, Any]]:
    """Fetch the next page of responses after the cursor (id-ordered)."""
    safe_limit = max(1, min(int(limit), 2000))
    with get_conn() as conn:
        with conn.cursor() as cur:
            if last_response_id is not None:
                cur.execute(
                    """
                    SELECT
                        response.id AS response_id,
                        response.inbound_id,
                        response.reply_text AS agent_reply,
                        response.intent,
                        response.handoff_required,
                        response.safety_reason,
                        response.response_metadata,
                        response.created_at AS response_created_at,
                        response.sender_key,
                        inbound.text AS customer_text,
                        inbound.channel,
                        inbound.conversation_id,
                        inbound.sender_phone
                    FROM public.ai_agent_responses AS response
                    LEFT JOIN public.ai_inbound_messages AS inbound
                      ON inbound.id = response.inbound_id
                    WHERE response.id > %s
                    ORDER BY response.id ASC
                    LIMIT %s
                    """,
                    (int(last_response_id), safe_limit),
                )
            else:
                since = datetime.now(timezone.utc) - timedelta(
                    hours=max(1, int(bootstrap_hours))
                )
                cur.execute(
                    """
                    SELECT
                        response.id AS response_id,
                        response.inbound_id,
                        response.reply_text AS agent_reply,
                        response.intent,
                        response.handoff_required,
                        response.safety_reason,
                        response.response_metadata,
                        response.created_at AS response_created_at,
                        response.sender_key,
                        inbound.text AS customer_text,
                        inbound.channel,
                        inbound.conversation_id,
                        inbound.sender_phone
                    FROM public.ai_agent_responses AS response
                    LEFT JOIN public.ai_inbound_messages AS inbound
                      ON inbound.id = response.inbound_id
                    WHERE response.created_at >= %s
                    ORDER BY response.id ASC
                    LIMIT %s
                    """,
                    (since, safe_limit),
                )
            rows = list(cur.fetchall() or [])
    _ = tenant_id
    return [dict(row) for row in rows]
