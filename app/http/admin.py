"""Admin diagnostics that are not health or Instagram Story CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.security import verify_admin_token

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/integrity-kpis", dependencies=[Depends(verify_admin_token)])
async def admin_integrity_kpis(days: int = 7):
    """Assertiveness KPIs vs 30d targets (not_found / ambiguous / tray / factual)."""
    from app.ops.integrity_kpis import build_integrity_kpi_report

    return {"ok": True, **build_integrity_kpi_report(days=days)}


@router.post("/human-takeover/cleanup", dependencies=[Depends(verify_admin_token)])
async def admin_cleanup_human_takeover(stale_days: int = 7, limit: int = 500):
    """Purge stale rows from ai_human_takeover_state (does not mutate ChatBô conversas)."""
    from app.ops.human_takeover import cleanup_stale_takeover_state

    return cleanup_stale_takeover_state(stale_days=stale_days, limit=limit)


@router.get(
    "/orders/{order_id}/tracking-audit",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_order_tracking_audit(order_id: str):
    from app.commerce.order_tracking_audit import audit_order_tracking
    from app.tray.tray_tools import execute_tool

    return {
        "ok": True,
        **await audit_order_tracking(order_id, execute=execute_tool),
    }
