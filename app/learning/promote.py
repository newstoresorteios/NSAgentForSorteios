"""Promote learning insights to instruction extensions (canary auto-activate)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_conn, to_jsonb
from app.learning.constitution import check_instruction_delta, constitution_metadata
from app.persona.instruction_extension_repository import (
    approve_extension,
    create_extension_proposal,
)


def mark_insight_status(
    *,
    insight_id: int,
    status: str,
    applied_extension_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if metadata is not None:
                cur.execute(
                    """
                    UPDATE public.ai_learning_insights
                    SET status = %s,
                        applied_extension_id = COALESCE(%s, applied_extension_id),
                        reviewed_at = now(),
                        updated_at = %s,
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        applied_extension_id,
                        now,
                        to_jsonb(metadata),
                        insight_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE public.ai_learning_insights
                    SET status = %s,
                        applied_extension_id = COALESCE(%s, applied_extension_id),
                        reviewed_at = now(),
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (status, applied_extension_id, now, insight_id),
                )


def promote_insights_to_extensions(
    *,
    tenant_id: str,
    insight_id: int,
    category: str,
    insight_text: str,
    confidence: float,
    importance: float,
    baseline_fail_rate: float | None = None,
    baseline_reviews: int | None = None,
    reviewed: bool = False,
) -> int | None:
    """Create an instruction extension. Activate only after explicit review."""
    settings = get_settings()
    ok, reason = check_instruction_delta(insight_text)
    if not ok:
        mark_insight_status(
            insight_id=insight_id,
            status="rejected",
            metadata=constitution_metadata(reason),
        )
        print("[attendance.learning.constitution_rejected]", {
            "insight_id": insight_id,
            "reason": reason,
        })
        return None

    extension_key = f"learning:{category}:{insight_id}"
    canary_hours = int(getattr(settings, "agent_learning_canary_hours", 6) or 6)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, canary_hours))
    auto_activate = bool(getattr(settings, "agent_learning_auto_activate", False)) and bool(
        reviewed
    )
    meta = {
        "source": "attendance_learning",
        "insight_id": insight_id,
        "canary": auto_activate,
        "baseline_fail_rate": baseline_fail_rate,
        "baseline_reviews": baseline_reviews,
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        created = create_extension_proposal(
            tenant_id=tenant_id,
            extension_key=extension_key,
            instruction_text=insight_text,
            category=category,
            scope="tenant",
            source="model_proposal",
            importance=importance,
            confidence=confidence,
            metadata=meta,
        )
    except Exception as exc:
        print("[attendance.learning.extension_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return None
    extension_id = created.get("id") if isinstance(created, dict) else None
    activated = False
    if extension_id and auto_activate:
        try:
            approved = approve_extension(
                int(extension_id),
                tenant_id=tenant_id,
                approved_by="learning_auto",
                expires_at=expires_at,
            )
            extension_id = int(approved.get("id") or extension_id)
            activated = True
        except Exception as exc:
            print("[attendance.learning.activate_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
                "extension_id": extension_id,
            })
    if extension_id:
        if activated:
            mark_insight_status(
                insight_id=insight_id,
                status="applied",
                applied_extension_id=int(extension_id),
                metadata={"canary_expires_at": expires_at.isoformat()},
            )
        else:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.ai_learning_insights
                        SET applied_extension_id = %s,
                            updated_at = now()
                        WHERE id = %s
                          AND status = 'pending_review'
                        """,
                        (extension_id, insight_id),
                    )
        print("[attendance.learning.promote]", {
            "insight_id": insight_id,
            "extension_id": extension_id,
            "activated": activated,
        })
    return extension_id
