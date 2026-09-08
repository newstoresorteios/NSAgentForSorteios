"""Poll TrayAdaptor webhook events and apply catalog / PIX side effects."""

from __future__ import annotations

from typing import Any

from app.catalog.index.invalidate import (
    delete_index_product,
    invalidate_snapshot,
    refresh_index_product,
    refresh_index_variant,
)
from app.commerce import pix_payment_repository as repo
from app.db import ensure_tables, get_conn
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError

_CURSOR_KEY = "tray_webhooks"
_PRODUCT_SCOPES = frozenset(
    {"product", "product_price", "product_stock"}
)
_VARIANT_SCOPES = frozenset(
    {"variant", "variant_price", "variant_stock"}
)
_ORDER_SCOPES = frozenset({"order"})
_MAX_REFRESH = 10


def load_webhook_cursor() -> int:
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT last_event_id
                    FROM public.ai_tray_sync_cursors
                    WHERE cursor_key = %s
                    LIMIT 1
                    """,
                    (_CURSOR_KEY,),
                )
                row = cur.fetchone()
        if not row:
            return 0
        return int(row.get("last_event_id") or 0)
    except Exception:
        # An unavailable checkpoint is not a new consumer. Let the caller retry.
        raise


def save_webhook_cursor(event_id: int) -> None:
    try:
        ensure_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_tray_sync_cursors (cursor_key, last_event_id, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (cursor_key) DO UPDATE SET
                        last_event_id = GREATEST(ai_tray_sync_cursors.last_event_id, EXCLUDED.last_event_id),
                        updated_at = now()
                    """,
                    (_CURSOR_KEY, int(event_id)),
                )
    except Exception as exc:
        print("[tray.webhook.cursor.save_failed]", {"error_type": type(exc).__name__})
        raise


def _session_id_from_order(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    session = order.get("session_id")
    text = str(session or "").strip()
    return text or None


async def confirm_pix_for_tray_order(
    order_id: str,
    *,
    client: TrayAdapterClient | None = None,
) -> dict[str, Any]:
    oid = str(order_id or "").strip()
    if not oid:
        return {"ok": False, "reason": "missing_order_id"}
    existing = repo.get_pix_payment_by_tray_order_id(oid)
    if existing and str(existing.get("settlement_status") or "") == "completed":
        return {
            "ok": True,
            "action": "already_settled",
            "tray_order_id": oid,
            "payment_id": existing.get("mp_payment_id"),
        }
    tray = client or TrayAdapterClient()
    try:
        order = await tray.get_order(oid)
    except TrayAdapterError as exc:
        return {"ok": False, "reason": "tray_get_failed", "status_code": exc.status_code}
    session_id = _session_id_from_order(order)
    row = (
        repo.get_pix_payment_by_cart_session_id(session_id)
        if session_id
        else None
    )
    if not row:
        return {"ok": True, "action": "ignored", "reason": "pix_row_missing", "tray_order_id": oid}
    if str(row.get("status") or "").lower() != "approved":
        return {
            "ok": True,
            "action": "ignored",
            "reason": "pix_not_approved",
            "payment_id": row.get("mp_payment_id"),
        }
    if row.get("tray_order_id") and str(row.get("settlement_status") or "") == "completed":
        return {
            "ok": True,
            "action": "already_settled",
            "payment_id": row.get("mp_payment_id"),
            "tray_order_id": row.get("tray_order_id"),
        }
    from app.commerce.pix_settlement import validate_existing_pix_order

    rejection = await validate_existing_pix_order(row, order)
    if rejection:
        return {"ok": False, "reason": rejection, "tray_order_id": oid}
    updated = repo.mark_pix_settlement(
        str(row["mp_payment_id"]),
        settlement_status="completed",
        tray_order_id=oid,
        settlement_error=None,
    )
    print("[tray.webhook.pix.confirmed]", {
        "payment_id": row.get("mp_payment_id"),
        "tray_order_id": oid,
    })
    return {
        "ok": True,
        "action": "confirmed",
        "payment_id": row.get("mp_payment_id"),
        "tray_order_id": oid,
        "settlement_status": (updated or {}).get("settlement_status") or "completed",
    }


async def apply_tray_webhook_event(
    event: dict[str, Any],
    *,
    client: TrayAdapterClient | None = None,
    refresh_budget: list[int] | None = None,
) -> dict[str, Any]:
    scope = str(event.get("scope_name") or "").strip().lower()
    scope_id = str(event.get("scope_id") or "").strip()
    act = str(event.get("act") or "").strip().lower()
    tray = client or TrayAdapterClient()
    if scope in _PRODUCT_SCOPES:
        if act == "delete":
            invalidate_snapshot(product_id=scope_id, client=tray)
            deleted = delete_index_product(scope_id)
            return {"ok": True, "action": "deleted", "product_id": scope_id, "deleted": deleted}
        budget = refresh_budget if refresh_budget is not None else [0]
        if budget[0] >= _MAX_REFRESH:
            invalidate_snapshot(product_id=scope_id, client=tray)
            return {"ok": True, "action": "invalidated", "product_id": scope_id}
        budget[0] += 1
        result = await refresh_index_product(scope_id, client=tray)
        result["action"] = "refreshed" if result.get("ok") else "refresh_failed"
        return result
    if scope in _VARIANT_SCOPES:
        budget = refresh_budget if refresh_budget is not None else [0]
        if act == "delete" or budget[0] >= _MAX_REFRESH:
            return await refresh_index_variant(scope_id, client=tray)
        budget[0] += 1
        result = await refresh_index_variant(scope_id, client=tray)
        result["action"] = result.get("action") or "refreshed"
        return result
    if scope in _ORDER_SCOPES:
        return await confirm_pix_for_tray_order(scope_id, client=tray)
    return {"ok": True, "action": "ignored", "scope_name": scope}


async def consume_tray_webhook_events(
    *,
    client: TrayAdapterClient | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    tray = client or TrayAdapterClient()
    cursor = load_webhook_cursor()
    # Ask for the oldest unprocessed page, including bootstrap. A latest-page
    # bootstrap can silently skip failed events as newer events arrive.
    since_id = cursor
    try:
        payload = await tray.list_webhook_events(limit=limit, since_id=since_id)
    except TrayAdapterError as exc:
        return {
            "ok": False,
            "reason": "adapter_unavailable",
            "status_code": exc.status_code,
            "cursor": cursor,
        }
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or payload.get("error") or payload.get("success") is False:
        return {"ok": False, "reason": "invalid_event_page", "cursor": cursor}
    if any(not isinstance(event, dict) or not str(event.get("id", "")).isdigit() for event in events):
        return {"ok": False, "reason": "invalid_event_page", "cursor": cursor}
    events.sort(key=lambda event: int(event["id"]))
    applied = []
    budget = [0]
    max_id = cursor
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = int(event.get("id") or 0)
        if event_id <= cursor:
            continue
        try:
            applied.append(
                await apply_tray_webhook_event(
                    event, client=tray, refresh_budget=budget
                )
            )
        except Exception as exc:  # noqa: BLE001
            applied.append({"ok": False, "error_type": type(exc).__name__})
        if not applied[-1].get("ok"):
            # Only acknowledge a contiguous successful prefix. Both exceptions
            # and explicit failures must stay available on the next poll.
            break
        if event_id > max_id:
            max_id = event_id
    if max_id > cursor:
        save_webhook_cursor(max_id)
    return {
        "ok": all(result.get("ok") for result in applied),
        "processed": len(applied),
        "cursor": max_id,
        "bootstrap": cursor == 0,
        "results": applied,
    }
