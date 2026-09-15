"""Continuous attendance learning: cursor → diagnose → reflect → canary."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_conn, get_returning_id, to_jsonb
from app.learning.cases import upsert_learning_case
from app.learning.constitution import check_instruction_delta, constitution_metadata
from app.learning.cursor import fetch_attendances_since, load_cursor, save_cursor
from app.learning.diagnose import (
    PIPELINE_CAPTURE_REASONS,
    aggregate_failures,
    classify_attendance,
    classify_pipeline_block,
    cluster_is_high_signal,
    group_by_conversation,
    insight_category_for,
)
from app.learning.promote import mark_insight_status, promote_insights_to_extensions
from app.learning.reflect import reflect_cluster
from app.learning.rollback import compute_fail_rate, evaluate_canaries


# Re-export for pipeline + tests.
__all__ = (
    "PIPELINE_CAPTURE_REASONS",
    "classify_attendance",
    "classify_pipeline_block",
    "promote_insights_to_extensions",
    "record_pipeline_block_review",
    "attach_response_id_to_pipeline_reviews",
    "fetch_recent_reviews_for_cluster",
    "run_attendance_learning_batch",
)


def _insight_key(category: str, title: str) -> str:
    digest = hashlib.sha256(f"{category}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"{category}:{digest}"


def _workspace_id(row: dict[str, Any]) -> str | None:
    direct = str(row.get("workspace_id") or "").strip()
    if direct:
        return direct
    metadata = row.get("response_metadata")
    if not isinstance(metadata, dict):
        return None
    runtime = metadata.get("persona_runtime")
    if not isinstance(runtime, dict):
        return None
    value = str(runtime.get("workspace_id") or "").strip()
    return value or None


def fetch_recent_attendances(
    *,
    tenant_id: str,
    lookback_hours: int = 24,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Bootstrap-style fetch used by tests; production batch uses the cursor."""
    return fetch_attendances_since(
        tenant_id=tenant_id,
        last_response_id=None,
        limit=limit,
        bootstrap_hours=lookback_hours,
    )


def persist_attendance_review(
    *,
    tenant_id: str,
    row: dict[str, Any],
    classification: dict[str, Any],
) -> tuple[int | None, bool]:
    """Insert a review. Returns (id, created). Idempotent on (tenant, response_id)."""
    response_id = row.get("response_id")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_attendance_reviews (
                    tenant_id, workspace_id, conversation_key, sender_key,
                    inbound_id, response_id, channel,
                    customer_text, agent_reply, outcome,
                    failure_codes, signals
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, response_id) WHERE response_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id,
                    _workspace_id(row),
                    row.get("conversation_id"),
                    row.get("sender_key"),
                    row.get("inbound_id"),
                    response_id,
                    row.get("channel"),
                    row.get("customer_text"),
                    row.get("agent_reply"),
                    classification["outcome"],
                    to_jsonb(classification["failure_codes"]),
                    to_jsonb(classification["signals"]),
                ),
            )
            inserted = cur.fetchone()
            if inserted:
                return get_returning_id(inserted), True
            if response_id is None:
                return None, False
            cur.execute(
                """
                SELECT id
                FROM public.ai_attendance_reviews
                WHERE tenant_id = %s AND response_id = %s
                ORDER BY id ASC
                LIMIT 1
                """,
                (tenant_id, response_id),
            )
            existing = cur.fetchone()
            return get_returning_id(existing), False


def record_pipeline_block_review(
    *,
    tenant_id: str | None = None,
    conversation_key: str | None = None,
    sender_key: str | None = None,
    inbound_id: int | None = None,
    response_id: int | None = None,
    channel: str | None = None,
    customer_text: str | None = None,
    agent_reply: str | None = None,
    safety_reason: str | None = None,
    intent: str | None = None,
    result_metadata: dict[str, Any] | None = None,
) -> int | None:
    reason = str(safety_reason or "").strip()
    if reason not in PIPELINE_CAPTURE_REASONS:
        return None
    settings = get_settings()
    resolved_tenant = str(
        tenant_id
        or getattr(settings, "agent_persona_tenant_id", "newstore")
        or "newstore"
    )
    classification = classify_pipeline_block(
        safety_reason=reason,
        result_metadata=result_metadata,
        intent=intent,
        channel=channel,
    )
    row = {
        "workspace_id": (
            (result_metadata or {}).get("persona_runtime", {}).get("workspace_id")
            if isinstance((result_metadata or {}).get("persona_runtime"), dict)
            else None
        ),
        "conversation_id": conversation_key,
        "sender_key": sender_key,
        "inbound_id": inbound_id,
        "response_id": response_id,
        "channel": channel,
        "customer_text": customer_text,
        "agent_reply": agent_reply,
        "intent": intent,
        "safety_reason": reason,
    }
    try:
        persisted = persist_attendance_review(
            tenant_id=resolved_tenant,
            row=row,
            classification=classification,
        )
        if isinstance(persisted, tuple):
            review_id, _created = persisted
        else:
            review_id = persisted
    except Exception as exc:
        print("[attendance.learning.pipeline_review_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
            "safety_reason": reason,
        })
        return None
    if review_id is not None:
        print("[attendance.learning.pipeline_review]", {
            "review_id": review_id,
            "safety_reason": reason,
            "outcome": classification["outcome"],
        })
    return review_id


def attach_response_id_to_pipeline_reviews(
    *,
    inbound_id: int | None,
    response_id: int | None,
    tenant_id: str | None = None,
) -> int:
    """Link gate reviews written before persist to the saved agent response."""
    if inbound_id is None or response_id is None:
        return 0
    settings = get_settings()
    if not getattr(settings, "database_url", None):
        return 0
    resolved_tenant = str(
        tenant_id
        or getattr(settings, "agent_persona_tenant_id", "newstore")
        or "newstore"
    )
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.ai_attendance_reviews
                    SET response_id = %s
                    WHERE tenant_id = %s
                      AND inbound_id = %s
                      AND response_id IS NULL
                    """,
                    (int(response_id), resolved_tenant, int(inbound_id)),
                )
                return int(cur.rowcount or 0)
    except Exception as exc:
        print("[attendance.learning.attach_response_id_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return 0


def fetch_recent_reviews_for_cluster(
    *,
    tenant_id: str,
    lookback_hours: int = 24,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Load recent reviews (including pipeline blocks without a new cursor row)."""
    if not getattr(get_settings(), "database_url", None):
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=max(int(lookback_hours), 1))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, conversation_key, customer_text, agent_reply,
                       outcome, failure_codes, workspace_id
                FROM public.ai_attendance_reviews
                WHERE tenant_id = %s
                  AND created_at >= %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (tenant_id, since, max(int(limit), 1)),
            )
            rows = cur.fetchall() or []
    reviews: list[dict[str, Any]] = []
    for row in rows:
        codes = row[5] if len(row) > 5 else []
        if isinstance(codes, str):
            import json

            try:
                codes = json.loads(codes)
            except Exception:
                codes = []
        if not isinstance(codes, list):
            codes = []
        reviews.append({
            "id": row[0],
            "conversation_id": row[1],
            "customer_text": row[2],
            "agent_reply": row[3],
            "outcome": row[4],
            "failure_codes": [str(item) for item in codes],
            "workspace_id": row[6] if len(row) > 6 else None,
        })
    return reviews


def upsert_learning_insight(
    *,
    tenant_id: str,
    workspace_id: str | None,
    category: str,
    title: str,
    insight_text: str,
    evidence_count: int,
    confidence: float,
    importance: float,
    source_review_ids: list[int],
    metadata: dict[str, Any] | None = None,
) -> int | None:
    key = _insight_key(category, title)
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_learning_insights (
                    tenant_id, workspace_id, insight_key, category, title, insight_text,
                    evidence_count, confidence, importance, status,
                    source_review_ids, metadata, first_seen_at, last_seen_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, 'pending_review',
                    %s, %s, %s, %s
                )
                ON CONFLICT (
                    tenant_id,
                    COALESCE(workspace_id, '00000000-0000-0000-0000-000000000000'::uuid),
                    insight_key
                ) WHERE status = 'pending_review'
                DO UPDATE SET
                    evidence_count = public.ai_learning_insights.evidence_count + EXCLUDED.evidence_count,
                    confidence = GREATEST(
                        public.ai_learning_insights.confidence,
                        EXCLUDED.confidence
                    ),
                    importance = GREATEST(
                        public.ai_learning_insights.importance,
                        EXCLUDED.importance
                    ),
                    last_seen_at = EXCLUDED.last_seen_at,
                    source_review_ids = public.ai_learning_insights.source_review_ids
                        || EXCLUDED.source_review_ids,
                    insight_text = EXCLUDED.insight_text,
                    updated_at = EXCLUDED.last_seen_at
                RETURNING id
                """,
                (
                    tenant_id,
                    workspace_id,
                    key,
                    category,
                    title,
                    insight_text,
                    evidence_count,
                    confidence,
                    importance,
                    to_jsonb(source_review_ids),
                    to_jsonb(metadata or {}),
                    now,
                    now,
                ),
            )
            return get_returning_id(cur.fetchone())


async def run_attendance_learning_batch(
    *,
    lookback_hours: int | None = None,
    limit: int | None = None,
    auto_promote: bool | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    runtime_values: dict[str, Any] = {}
    runtime_workspace_id: str | None = None
    try:
        from app.persona.persona_runtime import load_persona_runtime

        learning_runtime = load_persona_runtime()
        runtime_values = learning_runtime.runtime_configuration
        runtime_workspace_id = learning_runtime.workspace_id
    except Exception as exc:
        print("[attendance.learning.runtime_config_error]", {
            "error_type": type(exc).__name__, "error": str(exc)[:160]
        })

    if runtime_values.get("learningEnabled", True) is False:
        return {
            "ok": True,
            "skipped": "learning_disabled",
            "tenant_id": str(getattr(settings, "agent_persona_tenant_id", "newstore") or "newstore"),
            "workspace_id": runtime_workspace_id,
            "rows_scanned": 0,
        }
    tenant_id = str(getattr(settings, "agent_persona_tenant_id", "newstore") or "newstore")
    bootstrap_hours = lookback_hours or int(runtime_values.get("learningLookbackHours") or
        getattr(settings, "agent_learning_bootstrap_hours", None)
        or getattr(settings, "agent_learning_lookback_hours", 24)
        or 24
    )
    row_limit = limit or int(runtime_values.get("learningBatchLimit") or getattr(settings, "agent_learning_batch_limit", 500) or 500)
    if auto_promote is None:
        auto_apply = bool(runtime_values.get("learningAutoPromote", getattr(settings, "agent_learning_auto_promote", False)))
    else:
        auto_apply = bool(auto_promote)
    max_clusters = int(runtime_values.get("learningMaxClusters") or getattr(settings, "agent_learning_max_clusters", 5) or 5)
    canary_hours = int(runtime_values.get("learningCanaryHours") or getattr(settings, "agent_learning_canary_hours", 6) or 6)
    rollback_min_reviews = int(runtime_values.get("learningRollbackMinReviews") or getattr(settings, "agent_learning_rollback_min_reviews", 20) or 20)
    rollback_fail_lift = float(runtime_values.get("learningRollbackFailLift") or getattr(settings, "agent_learning_rollback_fail_lift", 1.2) or 1.2)
    auto_activate_enabled = bool(
        runtime_values.get(
            "learningAutoActivate",
            getattr(settings, "agent_learning_auto_activate", False),
        )
    )

    errors: list[str] = []
    rollback_summary = {"rolled_back": 0, "confirmed": 0, "extended": 0}
    try:
        rollback_summary = evaluate_canaries(
            tenant_id=tenant_id,
            min_reviews=rollback_min_reviews,
            fail_lift=rollback_fail_lift,
            canary_hours=canary_hours,
        )
    except Exception as exc:
        errors.append("canary_evaluation_failed")
        print("[attendance.learning.rollback_batch_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })

    cursor = {}
    try:
        cursor = load_cursor(tenant_id=tenant_id) or {}
    except Exception as exc:
        print("[attendance.learning.cursor_load_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return {"ok": False, "error": "cursor_load_failed", "rows_scanned": 0}
    cursor_from = cursor.get("last_response_id")
    try:
        rows = fetch_attendances_since(
            tenant_id=tenant_id,
            last_response_id=cursor_from,
            limit=row_limit,
            bootstrap_hours=bootstrap_hours,
        )
    except Exception as exc:
        print("[attendance.learning.fetch_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return {"ok": False, "error": "attendance_fetch_failed", "rows_scanned": 0}

    reviews: list[dict[str, Any]] = []
    last_response_id = cursor_from
    last_response_at = cursor.get("last_response_at")
    for row in rows:
        classification = classify_attendance(row)
        response_id = row.get("response_id")
        created_at = row.get("response_created_at")
        try:
            persisted = persist_attendance_review(
                tenant_id=tenant_id,
                row=row,
                classification=classification,
            )
            if isinstance(persisted, tuple):
                review_id, created = persisted
            else:
                review_id, created = persisted, True
        except Exception as exc:
            print("[attendance.learning.review_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
            })
            errors.append("review_persist_failed")
            break
        if review_id is None:
            errors.append("review_persist_missing_id")
            break
        if response_id is not None:
            last_response_id = int(response_id)
        if created_at is not None:
            last_response_at = created_at
        if not created:
            continue
        reviews.append({
            "id": review_id,
            "failure_codes": classification["failure_codes"],
            "outcome": classification["outcome"],
            "customer_text": row.get("customer_text"),
            "agent_reply": row.get("agent_reply"),
            "conversation_id": row.get("conversation_id"),
            "workspace_id": _workspace_id(row),
        })

    try:
        extras = fetch_recent_reviews_for_cluster(
            tenant_id=tenant_id,
            lookback_hours=bootstrap_hours,
            limit=row_limit,
        )
        seen = {item["id"] for item in reviews}
        for extra in extras:
            extra_id = extra.get("id")
            if extra_id is None or extra_id in seen:
                continue
            reviews.append(extra)
            seen.add(extra_id)
    except Exception as exc:
        errors.append("cluster_reviews_fetch_failed")
        print("[attendance.learning.cluster_reviews_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })

    if rows:
        try:
            save_cursor(
                tenant_id=tenant_id,
                last_response_id=last_response_id,
                last_response_at=last_response_at,
                metadata={"rows_scanned": len(rows)},
            )
        except Exception as exc:
            errors.append("cursor_save_failed")
            print("[attendance.learning.cursor_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
            })
    else:
        try:
            save_cursor(
                tenant_id=tenant_id,
                last_response_id=cursor_from,
                last_response_at=cursor.get("last_response_at"),
                metadata={"rows_scanned": 0},
            )
        except Exception:
            errors.append("cursor_save_failed")

    if errors:
        return {
            "ok": False, "errors": errors, "tenant_id": tenant_id,
            "rows_scanned": len(rows), "reviews_written": len(reviews),
            "cursor_from": cursor_from, "cursor_to": last_response_id,
            "extensions_promoted": 0, "activated": 0,
        }

    conversations = group_by_conversation(rows)
    buckets: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for workspace_review in reviews:
        for code, items in aggregate_failures([workspace_review]).items():
            buckets.setdefault((workspace_review.get("workspace_id"), code), []).extend(items)
    ranked = sorted(buckets.items(), key=lambda item: len(item[1]), reverse=True)
    ranked = ranked[:max_clusters]

    insights_created = 0
    extensions_created = 0
    reflections = 0
    constitution_rejected = 0
    activated = 0
    workspace_bundles = {}
    for (workspace_id, code), cluster_reviews in ranked:
        if not cluster_is_high_signal(code, len(cluster_reviews)):
            continue
        from app.configuration.repository import load_workspace_bundle
        from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
        if workspace_id:
            try:
                if workspace_id not in workspace_bundles:
                    workspace_bundles[workspace_id] = load_workspace_bundle(workspace_id)
                cluster_bundle = workspace_bundles[workspace_id]
            except Exception:
                errors.append("workspace_configuration_unavailable")
                continue
        else:
            # Legacy rows require explicit attribution before policy-dependent learning.
            errors.append("learning_workspace_unresolved")
            continue
        cluster_values = cluster_bundle.get("values") or {}
        if cluster_values.get("learningEnabled") is False:
            continue
        # Promotion and the baseline belong to the same workspace as the cluster.
        auto_apply = bool(cluster_values.get("learningAutoPromote", False)) if auto_promote is None else bool(auto_promote)
        auto_activate_enabled = bool(cluster_values.get("learningAutoActivate", False))
        canary_hours = int(cluster_values.get("learningCanaryHours", 6))
        try:
            baseline_rate, baseline_n = compute_fail_rate(tenant_id=tenant_id, workspace_id=workspace_id,
                since=datetime.now(timezone.utc) - timedelta(hours=24))
        except Exception:
            # Unknown baseline cannot justify automatic activation.
            baseline_rate, baseline_n = 0.0, 0
            auto_activate_enabled = False
        policy_tokens = bind_bundle(cluster_bundle, settings_from_bundle(settings, cluster_bundle))
        try:
            delta = await reflect_cluster(failure_code=code, reviews=cluster_reviews)
            if delta is None:
                continue
            reflections += 1
            ok, reason = check_instruction_delta(delta.instruction_delta, business_policy=cluster_values)
        finally:
            reset_bundle(policy_tokens)
        category = delta.category or insight_category_for(code)
        title = delta.title
        insight_text = delta.instruction_delta
        evidence = len(cluster_reviews)
        confidence = min(0.95, float(delta.confidence or 0.5))
        importance = min(0.95, 0.5 + 0.08 * evidence)
        review_ids = [int(item["id"]) for item in cluster_reviews if item.get("id") is not None]
        try:
            insight_id = upsert_learning_insight(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                category=category if category in {
                    "persona", "knowledge", "retrieval", "handoff",
                    "greeting", "policy", "other",
                } else "other",
                title=title,
                insight_text=insight_text,
                evidence_count=evidence,
                confidence=confidence,
                importance=importance,
                source_review_ids=review_ids[:40],
                metadata={"failure_code": code, "reflected": True},
            )
        except Exception as exc:
            print("[attendance.learning.insight_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
                "code": code,
            })
            continue
        if insight_id is None:
            continue
        insights_created += 1
        if not ok:
            mark_insight_status(
                insight_id=insight_id,
                status="rejected",
                metadata=constitution_metadata(reason),
            )
            constitution_rejected += 1
            continue
        if not auto_apply:
            continue
        ext_id = promote_insights_to_extensions(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            insight_id=insight_id,
            category=category,
            insight_text=insight_text,
            confidence=confidence,
            importance=importance,
            baseline_fail_rate=baseline_rate,
            baseline_reviews=baseline_n,
            canary_hours=canary_hours,
            reviewed=auto_activate_enabled,
            business_policy=cluster_values,
        )
        if ext_id:
            extensions_created += 1
            if auto_activate_enabled:
                activated += 1
            else:
                # A pending proposal must not enter the active experience bank.
                continue
            sample = cluster_reviews[0]
            try:
                upsert_learning_case(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    failure_code=code,
                    conversation_key=str(sample.get("conversation_id") or "") or None,
                    customer_excerpt=str(sample.get("customer_text") or ""),
                    bad_reply=str(sample.get("agent_reply") or ""),
                    correction=insight_text,
                    insight_id=insight_id,
                    importance=importance,
                )
            except Exception as exc:
                print("[attendance.learning.case_error]", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:160],
                    "code": code,
                })

    summary = {
        "ok": not errors,
        "errors": errors,
        "tenant_id": tenant_id,
        "lookback_hours": bootstrap_hours,
        "rows_scanned": len(rows),
        "reviews_written": len(reviews),
        "conversations": len(conversations),
        "cursor_from": cursor_from,
        "cursor_to": last_response_id,
        "clusters": {f"{workspace or 'legacy'}:{code}": len(v) for (workspace, code), v in ranked},
        "reflections": reflections,
        "insights_upserted": insights_created,
        "extensions_promoted": extensions_created,
        "activated": activated,
        "rolled_back": int(rollback_summary.get("rolled_back") or 0),
        "confirmed": int(rollback_summary.get("confirmed") or 0),
        "constitution_rejected": constitution_rejected,
        "failure_buckets": {f"{workspace or 'legacy'}:{code}": len(v) for (workspace, code), v in buckets.items()},
    }
    print("[attendance.learning.batch]", summary)
    return summary
