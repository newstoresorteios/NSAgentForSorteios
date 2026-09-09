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
    """Fetch one canonical delivered response per inbound after the cursor.

    Delivery attempts may create several response rows for the same inbound.  A
    failed attempt is not part of the customer conversation and must never
    become learning evidence.  Among valid delivered rows, the latest response
    id is canonical.  The review exclusion keeps a later duplicate delivery
    from relearning an inbound that was already processed successfully.
    """
    safe_limit = max(1, min(int(limit), 2000))
    projection = """
        response.id AS response_id,
        response.inbound_id,
        response.reply_text AS agent_reply,
        response.intent,
        response.handoff_required,
        response.safety_reason,
        COALESCE(
            response.provider_response->'_agent_metadata',
            response.provider_response->'_agent_context',
            '{}'::jsonb
        ) AS response_metadata,
        response.created_at AS response_created_at,
        response.sender_key,
        inbound.text AS customer_text,
        inbound.channel,
        inbound.conversation_id,
        inbound.sender_phone
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if last_response_id is not None:
                cur.execute(
                    f"""
                    WITH ranked_deliveries AS (
                        SELECT
                            {projection},
                            ROW_NUMBER() OVER (
                                PARTITION BY response.inbound_id
                                ORDER BY response.id DESC
                            ) AS canonical_rank
                        FROM public.ai_agent_responses AS response
                        INNER JOIN public.ai_inbound_messages AS inbound
                          ON inbound.id = response.inbound_id
                        WHERE response.id > %s
                          AND response.provider_send_ok = true
                          AND NULLIF(TRIM(response.reply_text), '') IS NOT NULL
                          AND LOWER(COALESCE(
                              response.provider_response->>'skipped', 'false'
                          )) NOT IN ('true', '1')
                          AND LOWER(COALESCE(
                              response.provider_response->>'dry_run', 'false'
                          )) NOT IN ('true', '1')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM public.ai_attendance_reviews AS review
                              INNER JOIN public.ai_agent_responses AS reviewed_response
                                ON reviewed_response.id = review.response_id
                              WHERE review.tenant_id = %s
                                AND reviewed_response.inbound_id = response.inbound_id
                                AND reviewed_response.provider_send_ok = true
                                AND NULLIF(
                                    TRIM(reviewed_response.reply_text), ''
                                ) IS NOT NULL
                                AND LOWER(COALESCE(
                                    reviewed_response.provider_response->>'skipped',
                                    'false'
                                )) NOT IN ('true', '1')
                                AND LOWER(COALESCE(
                                    reviewed_response.provider_response->>'dry_run',
                                    'false'
                                )) NOT IN ('true', '1')
                          )
                    )
                    SELECT
                        response_id, inbound_id, agent_reply, intent,
                        handoff_required, safety_reason, response_metadata,
                        response_created_at, sender_key, customer_text,
                        channel, conversation_id, sender_phone
                    FROM ranked_deliveries
                    WHERE canonical_rank = 1
                    ORDER BY response_id ASC
                    LIMIT %s
                    """,
                    (int(last_response_id), tenant_id, safe_limit),
                )
            else:
                since = datetime.now(timezone.utc) - timedelta(
                    hours=max(1, int(bootstrap_hours))
                )
                cur.execute(
                    f"""
                    WITH ranked_deliveries AS (
                        SELECT
                            {projection},
                            ROW_NUMBER() OVER (
                                PARTITION BY response.inbound_id
                                ORDER BY response.id DESC
                            ) AS canonical_rank
                        FROM public.ai_agent_responses AS response
                        INNER JOIN public.ai_inbound_messages AS inbound
                          ON inbound.id = response.inbound_id
                        WHERE response.created_at >= %s
                          AND response.provider_send_ok = true
                          AND NULLIF(TRIM(response.reply_text), '') IS NOT NULL
                          AND LOWER(COALESCE(
                              response.provider_response->>'skipped', 'false'
                          )) NOT IN ('true', '1')
                          AND LOWER(COALESCE(
                              response.provider_response->>'dry_run', 'false'
                          )) NOT IN ('true', '1')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM public.ai_attendance_reviews AS review
                              INNER JOIN public.ai_agent_responses AS reviewed_response
                                ON reviewed_response.id = review.response_id
                              WHERE review.tenant_id = %s
                                AND reviewed_response.inbound_id = response.inbound_id
                                AND reviewed_response.provider_send_ok = true
                                AND NULLIF(
                                    TRIM(reviewed_response.reply_text), ''
                                ) IS NOT NULL
                                AND LOWER(COALESCE(
                                    reviewed_response.provider_response->>'skipped',
                                    'false'
                                )) NOT IN ('true', '1')
                                AND LOWER(COALESCE(
                                    reviewed_response.provider_response->>'dry_run',
                                    'false'
                                )) NOT IN ('true', '1')
                          )
                    )
                    SELECT
                        response_id, inbound_id, agent_reply, intent,
                        handoff_required, safety_reason, response_metadata,
                        response_created_at, sender_key, customer_text,
                        channel, conversation_id, sender_phone
                    FROM ranked_deliveries
                    WHERE canonical_rank = 1
                    ORDER BY response_id ASC
                    LIMIT %s
                    """,
                    (since, tenant_id, safe_limit),
                )
            rows = list(cur.fetchall() or [])
    return [dict(row) for row in rows]
