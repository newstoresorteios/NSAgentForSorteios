"""Conversation summary persistence (opt-in via feature flag)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db import get_conn, to_jsonb
from app.memory.memory_models import ConversationSummaryDelta


def close_resolved_questions(
    open_questions: list[str],
    resolved_points: list[str],
) -> list[str]:
    resolved_keys = {str(item).strip().casefold() for item in resolved_points if str(item).strip()}
    closed: list[str] = []
    for item in open_questions:
        text = str(item).strip()
        if text and text.casefold() not in resolved_keys and text not in closed:
            closed.append(text)
    return closed


def get_conversation_summary(
    *,
    tenant_id: str,
    conversation_key: str,
) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_conversation_summaries
                WHERE tenant_id = %s
                  AND conversation_key = %s
                LIMIT 1
                """,
                (tenant_id, conversation_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def apply_summary_delta(
    *,
    tenant_id: str,
    conversation_key: str,
    delta: ConversationSummaryDelta,
    inbound_id: int | None = None,
    response_id: int | None = None,
    max_chars: int = 2500,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    existing = get_conversation_summary(
        tenant_id=tenant_id,
        conversation_key=conversation_key,
    )

    def _merge(old: list[Any] | None, new: list[str], limit: int = 12) -> list[str]:
        merged: list[str] = []
        for item in list(old or []) + list(new or []):
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text[:240])
        return merged[-limit:]

    resolved = _merge(
        (existing or {}).get("resolved_points"),
        delta.resolved_points,
    )
    open_q = close_resolved_questions(
        _merge(
            (existing or {}).get("open_questions"),
            delta.open_questions,
        ),
        resolved,
    )
    corrections = _merge(
        (existing or {}).get("user_corrections"),
        delta.user_corrections,
    )
    commitments = _merge(
        (existing or {}).get("commitments"),
        delta.commitments,
    )
    current_goal = (delta.current_goal or (existing or {}).get("current_goal") or "")[
        :240
    ] or None
    last_failure = delta.last_failure or (existing or {}).get("last_failure")
    summary_parts = [
        f"goal={current_goal}" if current_goal else "",
        f"open={'; '.join(open_q[:4])}" if open_q else "",
        f"resolved={'; '.join(resolved[:4])}" if resolved else "",
    ]
    summary = "; ".join(part for part in summary_parts if part)[:max_chars]
    token_approx = max(1, len(summary) // 4) if summary else 0
    expected_version = (existing or {}).get("version")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if existing:
                cur.execute(
                    """
                    UPDATE public.ai_conversation_summaries
                    SET version = version + 1,
                        current_goal = %s,
                        summary = %s,
                        resolved_points = %s,
                        open_questions = %s,
                        user_corrections = %s,
                        commitments = %s,
                        last_failure = %s,
                        last_inbound_id = %s,
                        last_response_id = %s,
                        approximate_token_count = %s,
                        updated_at = %s
                    WHERE tenant_id = %s
                      AND conversation_key = %s
                      AND version = %s
                    RETURNING *
                    """,
                    (
                        current_goal,
                        summary,
                        to_jsonb(resolved),
                        to_jsonb(open_q),
                        to_jsonb(corrections),
                        to_jsonb(commitments),
                        last_failure,
                        inbound_id,
                        response_id,
                        token_approx,
                        now,
                        tenant_id,
                        conversation_key,
                        expected_version,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO public.ai_conversation_summaries (
                        tenant_id, conversation_key, current_goal, summary,
                        resolved_points, open_questions, user_corrections,
                        commitments, last_failure, last_inbound_id,
                        last_response_id, approximate_token_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        tenant_id,
                        conversation_key,
                        current_goal,
                        summary,
                        to_jsonb(resolved),
                        to_jsonb(open_q),
                        to_jsonb(corrections),
                        to_jsonb(commitments),
                        last_failure,
                        inbound_id,
                        response_id,
                        token_approx,
                    ),
                )
            row = cur.fetchone()
    if existing and not row:
        current = get_conversation_summary(
            tenant_id=tenant_id,
            conversation_key=conversation_key,
        ) or dict(existing)
        current["cas_conflict"] = True
        return current
    return dict(row or {})
