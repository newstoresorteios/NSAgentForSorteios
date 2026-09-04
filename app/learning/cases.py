"""Experience-replay case bank injected into the Crono prompt."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db import get_conn, get_returning_id, to_jsonb


def upsert_learning_case(
    *,
    tenant_id: str,
    failure_code: str,
    conversation_key: str | None,
    customer_excerpt: str,
    bad_reply: str,
    correction: str,
    insight_id: int | None = None,
    importance: float = 0.5,
) -> int | None:
    now = datetime.now(timezone.utc)
    case_key = f"learning:{failure_code}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_learning_cases (
                    tenant_id, case_key, conversation_key, failure_codes,
                    customer_excerpt, bad_reply, correction, status,
                    insight_id, importance, created_at, updated_at, metadata
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, 'active',
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, case_key) DO UPDATE SET
                    conversation_key = EXCLUDED.conversation_key,
                    failure_codes = EXCLUDED.failure_codes,
                    customer_excerpt = EXCLUDED.customer_excerpt,
                    bad_reply = EXCLUDED.bad_reply,
                    correction = EXCLUDED.correction,
                    status = 'active',
                    insight_id = COALESCE(EXCLUDED.insight_id, public.ai_learning_cases.insight_id),
                    importance = GREATEST(
                        public.ai_learning_cases.importance,
                        EXCLUDED.importance
                    ),
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata
                RETURNING id
                """,
                (
                    tenant_id,
                    case_key,
                    conversation_key,
                    to_jsonb([failure_code]),
                    (customer_excerpt or "")[:400],
                    (bad_reply or "")[:400],
                    (correction or "")[:800],
                    insight_id,
                    importance,
                    now,
                    now,
                    to_jsonb({"failure_code": failure_code}),
                ),
            )
            return get_returning_id(cur.fetchone())


def list_active_cases(
    *,
    tenant_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    safe_limit = max(0, min(int(limit), 20))
    if safe_limit == 0:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_learning_cases
                WHERE tenant_id = %s
                  AND status = 'active'
                ORDER BY importance DESC NULLS LAST, updated_at DESC
                LIMIT %s
                """,
                (tenant_id, safe_limit),
            )
            return [dict(row) for row in (cur.fetchall() or [])]


def format_learned_cases_block(
    cases: list[dict[str, Any]],
    *,
    max_chars: int = 2000,
) -> str:
    if not cases:
        return "<learned_cases>\n</learned_cases>"
    lines = ["<learned_cases>"]
    used = 0
    for item in cases:
        customer = (item.get("customer_excerpt") or "").strip()
        bad = (item.get("bad_reply") or "").strip()
        correction = (item.get("correction") or "").strip()
        if not correction:
            continue
        chunk = f"- cliente: {customer}\n  evite: {bad}\n  faça: {correction}"
        if used + len(chunk) > max_chars:
            break
        lines.append(chunk)
        used += len(chunk)
    lines.append("</learned_cases>")
    return "\n".join(lines)
