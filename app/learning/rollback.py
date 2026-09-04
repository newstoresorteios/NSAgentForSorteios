"""Canary KPI rollback for auto-activated learning extensions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_conn
from app.learning.promote import mark_insight_status
from app.persona.instruction_extension_repository import (
    clear_extension_expiry,
    list_learning_auto_extensions,
    supersede_extension,
)


def compute_fail_rate(
    *,
    tenant_id: str,
    since: datetime,
    until: datetime | None = None,
) -> tuple[float, int]:
    until = until or datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE COALESCE(jsonb_array_length(failure_codes), 0) > 0
                    ) AS failed
                FROM public.ai_attendance_reviews
                WHERE tenant_id = %s
                  AND created_at >= %s
                  AND created_at < %s
                """,
                (tenant_id, since, until),
            )
            row = cur.fetchone() or {}
    total = int(row.get("total") or 0)
    failed = int(row.get("failed") or 0)
    if total <= 0:
        return 0.0, 0
    return failed / total, total


def evaluate_canaries(*, tenant_id: str) -> dict[str, int]:
    settings = get_settings()
    min_reviews = int(getattr(settings, "agent_learning_rollback_min_reviews", 20) or 20)
    lift = float(getattr(settings, "agent_learning_rollback_fail_lift", 1.2) or 1.2)
    canary_hours = int(getattr(settings, "agent_learning_canary_hours", 6) or 6)
    now = datetime.now(timezone.utc)
    rolled_back = 0
    confirmed = 0
    extended = 0
    for row in list_learning_auto_extensions(tenant_id=tenant_id):
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = {}
        activated_raw = metadata.get("activated_at")
        try:
            activated_at = (
                datetime.fromisoformat(str(activated_raw).replace("Z", "+00:00"))
                if activated_raw
                else row.get("approved_at") or row.get("created_at")
            )
        except ValueError:
            activated_at = row.get("approved_at") or now
        if activated_at is not None and activated_at.tzinfo is None:
            activated_at = activated_at.replace(tzinfo=timezone.utc)
        baseline = metadata.get("baseline_fail_rate")
        try:
            baseline_rate = float(baseline) if baseline is not None else 0.0
        except (TypeError, ValueError):
            baseline_rate = 0.0
        fail_rate, n_reviews = compute_fail_rate(
            tenant_id=tenant_id,
            since=activated_at or (now - timedelta(hours=canary_hours)),
            until=now,
        )
        extension_id = int(row["id"])
        insight_id = metadata.get("insight_id")
        expires_at = row.get("expires_at")
        enough = n_reviews >= min_reviews
        worse = enough and baseline_rate > 0 and fail_rate > baseline_rate * lift
        if worse:
            try:
                supersede_extension(
                    extension_id,
                    tenant_id=tenant_id,
                    reason=(
                        f"kpi_rollback fail_rate={fail_rate:.3f} "
                        f"baseline={baseline_rate:.3f} n={n_reviews}"
                    ),
                )
                if insight_id is not None:
                    mark_insight_status(
                        insight_id=int(insight_id),
                        status="expired",
                        metadata={
                            "rollback": True,
                            "fail_rate": fail_rate,
                            "baseline_fail_rate": baseline_rate,
                            "reviews": n_reviews,
                        },
                    )
                rolled_back += 1
                print("[attendance.learning.rollback]", {
                    "extension_id": extension_id,
                    "fail_rate": round(fail_rate, 4),
                    "baseline": round(baseline_rate, 4),
                    "reviews": n_reviews,
                })
            except Exception as exc:
                print("[attendance.learning.rollback_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                    "extension_id": extension_id,
                })
            continue
        if enough and not worse:
            try:
                clear_extension_expiry(extension_id, tenant_id=tenant_id)
                confirmed += 1
            except Exception as exc:
                print("[attendance.learning.confirm_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                    "extension_id": extension_id,
                })
            continue
        if expires_at is not None:
            if getattr(expires_at, "tzinfo", None) is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now + timedelta(minutes=30) and not enough:
                try:
                    from app.persona.instruction_extension_repository import (
                        extend_extension_expiry,
                    )

                    extend_extension_expiry(
                        extension_id,
                        tenant_id=tenant_id,
                        expires_at=now + timedelta(hours=max(1, canary_hours)),
                    )
                    extended += 1
                except Exception as exc:
                    print("[attendance.learning.extend_error]", {
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:160],
                        "extension_id": extension_id,
                    })
    return {
        "rolled_back": rolled_back,
        "confirmed": confirmed,
        "extended": extended,
    }
