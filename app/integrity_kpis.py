"""Integrity / assertiveness KPIs from ai_agent_responses (+ ingress queues)."""

from __future__ import annotations

from typing import Any

from .config import get_settings
from .db import ensure_tables, get_conn

# Families mapped to the integrity plan targets.
_AMBIGUOUS = frozenset(
    {
        "ambiguous",
        "ambiguous_product",
        "product_ambiguous",
        "exact_product_ambiguous_brand",
        "ambiguous_purchase_item",
        "recommendation_near_match",
    }
)
_NOT_FOUND = frozenset(
    {
        "not_found",
        "product_not_found",
        "recommendation_no_match",
        "exact_product_not_found",
        "catalog_empty",
    }
)
_CLARIFY = frozenset({"commerce_clarification"})
_FACTUAL = frozenset({"factual_validation_failed"})
_TRAY = frozenset({"tray_adapter_unavailable", "tray_circuit_open"})
_COMPLIANCE = frozenset(
    {
        "product_media_dead_link",
        "compliance_preference_reresearch",
        "persona_compliance_rewrite",
    }
)
_ORDER_OPS = frozenset(
    {
        "order_notes_unavailable",
        "order_tracking_missing",
    }
)

_TARGETS = {
    "not_found_pct": 12.0,
    "ambiguous_pct": 10.0,
    "factual_fail_pct": 2.0,
    "tray_down_pct": 0.5,
}


def _pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(100.0 * part / total, 2)


def _classify(reason: str | None) -> str | None:
    if reason is None or reason == "":
        return "ok"
    key = str(reason).strip()
    if key in _NOT_FOUND:
        return "not_found"
    if key in _AMBIGUOUS:
        return "ambiguous"
    if key in _CLARIFY:
        return "clarification"
    if key in _FACTUAL:
        return "factual_fail"
    if key in _TRAY:
        return "tray_down"
    if key in _COMPLIANCE:
        return "compliance_applied"
    if key in _ORDER_OPS:
        return "order_ops"
    return "other"


def fetch_safety_reason_counts(*, days: int = 7) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.database_url:
        return []
    ensure_tables()
    window = max(1, min(int(days), 90))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT safety_reason, COUNT(*)::int AS n
                FROM public.ai_agent_responses
                WHERE created_at > now() - (%(days)s * interval '1 day')
                GROUP BY safety_reason
                ORDER BY n DESC NULLS LAST
                """,
                {"days": window},
            )
            rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(
                {
                    "safety_reason": row.get("safety_reason"),
                    "n": int(row.get("n") or 0),
                }
            )
        else:
            out.append({"safety_reason": row[0], "n": int(row[1] or 0)})
    return out


def fetch_queue_depths() -> dict[str, Any]:
    settings = get_settings()
    if not settings.database_url:
        return {"configured": False}
    ensure_tables()
    depths: dict[str, dict[str, int]] = {"inbox": {}, "outbox": {}}
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table, key in (
                ("ai_inbound_inbox", "inbox"),
                ("ai_outbound_outbox", "outbox"),
            ):
                try:
                    cur.execute(
                        f"""
                        SELECT status, COUNT(*)::int AS n
                        FROM public.{table}
                        GROUP BY status
                        """
                    )
                    for row in cur.fetchall() or []:
                        if isinstance(row, dict):
                            status = str(row.get("status") or "unknown")
                            depths[key][status] = int(row.get("n") or 0)
                        else:
                            depths[key][str(row[0] or "unknown")] = int(row[1] or 0)
                except Exception:
                    depths[key] = {"error": 1}
    return {"configured": True, **depths}


def build_integrity_kpi_report(*, days: int = 7) -> dict[str, Any]:
    """Aggregate assertiveness KPIs for admin / ops dashboards."""
    counts = fetch_safety_reason_counts(days=days)
    total = sum(int(item["n"]) for item in counts)
    buckets = {
        "ok": 0,
        "not_found": 0,
        "ambiguous": 0,
        "clarification": 0,
        "factual_fail": 0,
        "tray_down": 0,
        "compliance_applied": 0,
        "order_ops": 0,
        "other": 0,
    }
    for item in counts:
        family = _classify(item.get("safety_reason"))
        if family is None:
            continue
        buckets[family] = buckets.get(family, 0) + int(item["n"])

    rates = {
        "not_found_pct": _pct(buckets["not_found"], total),
        "ambiguous_pct": _pct(buckets["ambiguous"], total),
        "factual_fail_pct": _pct(buckets["factual_fail"], total),
        "tray_down_pct": _pct(buckets["tray_down"], total),
        "compliance_applied_pct": _pct(buckets["compliance_applied"], total),
        "ok_pct": _pct(buckets["ok"], total),
    }
    vs_target = {
        key: {
            "actual": rates[key],
            "target": _TARGETS[key],
            "ok": rates[key] <= _TARGETS[key],
        }
        for key in _TARGETS
    }
    return {
        "days": max(1, min(int(days), 90)),
        "total_responses": total,
        "buckets": buckets,
        "rates": rates,
        "vs_target": vs_target,
        "by_reason": counts[:40],
        "queues": fetch_queue_depths(),
        "async_ingress_enabled": bool(
            getattr(get_settings(), "agent_async_ingress_enabled", False)
        ),
    }
