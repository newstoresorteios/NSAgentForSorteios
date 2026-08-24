"""Ops helpers: audit Tray tracking fields and count missing-tracking replies."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .config import get_settings
from .db import ensure_tables, get_conn

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _tracking_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    shipment = payload.get("shipment")
    shipment = shipment if isinstance(shipment, dict) else {}
    shipping = payload.get("shipping")
    shipping = shipping if isinstance(shipping, dict) else {}
    tracking: dict[str, Any] = {}
    for source in (payload, shipment, shipping):
        for key in (
            "sending_code",
            "tracking_url",
            "sending_date",
            "estimated_delivery_date",
        ):
            if key not in tracking and source.get(key) not in (None, ""):
                tracking[key] = source[key]
    return tracking


def _is_shipped(payload: dict[str, Any]) -> bool:
    status_group = str(payload.get("status_group") or "").casefold()
    status = str(payload.get("status") or "").casefold()
    return status_group == "shipped" or "enviad" in status


async def audit_order_tracking(
    order_id: str,
    *,
    execute: ToolExecutor,
) -> dict[str, Any]:
    """Fetch get_order_complete and report whether tracking is present."""
    raw = await execute("get_order_complete", {"order_id": str(order_id)})
    if not isinstance(raw, dict):
        return {
            "order_id": str(order_id),
            "ok": False,
            "reason": "invalid_payload",
        }
    if raw.get("error"):
        return {
            "order_id": str(order_id),
            "ok": False,
            "reason": raw.get("error"),
            "status_code": raw.get("status_code"),
        }
    tracking = _tracking_from_payload(raw)
    shipped = _is_shipped(raw)
    missing = shipped and not tracking.get("sending_code") and not tracking.get(
        "tracking_url"
    )
    return {
        "order_id": str(raw.get("order_id") or raw.get("id") or order_id),
        "ok": True,
        "status": raw.get("status"),
        "status_group": raw.get("status_group"),
        "shipped": shipped,
        "tracking_present": bool(tracking),
        "tracking": tracking,
        "missing_tracking_when_shipped": missing,
    }


def _default_sample_order_ids() -> list[str]:
    raw = str(getattr(get_settings(), "order_tracking_audit_sample_ids", "") or "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def fetch_recent_missing_tracking_reply_count(*, days: int = 1) -> dict[str, Any]:
    settings = get_settings()
    if not settings.database_url:
        return {"configured": False, "count": 0}
    ensure_tables()
    window = max(1, min(int(days), 30))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)::int AS n
                FROM public.ai_agent_responses
                WHERE created_at > now() - (%(days)s * interval '1 day')
                  AND reply_text ILIKE %(pattern)s
                """,
                {
                    "days": window,
                    "pattern": "%rastreio%cadastrado%",
                },
            )
            row = cur.fetchone()
    count = int((row or {}).get("n") or 0) if isinstance(row, dict) else int(row[0] or 0)
    return {"configured": True, "days": window, "count": count}


async def run_order_tracking_audit_batch(
    *,
    execute: ToolExecutor,
    sample_order_ids: list[str] | None = None,
    db_days: int = 1,
) -> dict[str, Any]:
    """Cron-friendly batch: probe sample orders + count recent missing-tracking replies."""
    ids = sample_order_ids if sample_order_ids is not None else _default_sample_order_ids()
    audits: list[dict[str, Any]] = []
    for order_id in ids[:10]:
        audits.append(await audit_order_tracking(order_id, execute=execute))
    missing_shipped = [
        item for item in audits if item.get("missing_tracking_when_shipped")
    ]
    db_stats = fetch_recent_missing_tracking_reply_count(days=db_days)
    return {
        "sample_count": len(audits),
        "missing_tracking_when_shipped": missing_shipped,
        "audits": audits,
        "recent_missing_tracking_replies": db_stats,
        "alert": bool(missing_shipped or int(db_stats.get("count") or 0) > 0),
    }
