"""HTTP endpoints for Mercado Pago PIX webhook and status poll."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.commerce.mercadopago_client import MercadoPagoError
from app.commerce.pix_payment_repository import get_pix_payment_by_cart_session_id
from app.commerce.pix_payment_service import handle_mercadopago_webhook, refresh_pix_payment_status

router = APIRouter(prefix="/api/payments", tags=["payments-pix"])


def _verify_mp_signature(request: Request, secret: str) -> bool:
    signature = (request.headers.get("x-signature") or "").strip()
    request_id = (request.headers.get("x-request-id") or "").strip()
    if not signature or not request_id:
        return False
    parts = dict(item.split("=", 1) for item in signature.split(",") if "=" in item)
    ts = (parts.get("ts") or "").strip()
    token = (parts.get("v1") or "").strip()
    data_id = (
        request.query_params.get("data.id") or request.query_params.get("id") or ""
    ).strip()
    if not ts or not token:
        return False
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


def _webhook_http_status(result: dict[str, Any]) -> int:
    reason = str(result.get("reason") or "")
    if reason in {"mp_fetch_failed", "internal_error"}:
        return 503
    return 200


@router.post("/webhook")
async def mercadopago_pix_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    secret = str(getattr(settings, "mp_webhook_secret", "") or "").strip()
    if secret and not _verify_mp_signature(request, secret):
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=401)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except Exception as exc:
        from app.commerce import log_swallowed

        log_swallowed("pix.webhook_json", exc)
        payload = {}

    query: dict[str, Any] = dict(request.query_params)
    result = await handle_mercadopago_webhook(payload, query)
    return JSONResponse(result, status_code=_webhook_http_status(result))


@router.get("/status")
async def pix_payment_status_for_session(cart_session_id: str | None = None) -> JSONResponse:
    settings = get_settings()
    if not settings.resolved_mp_access_token():
        return JSONResponse({"error": "mp_token_missing"}, status_code=503)
    row = get_pix_payment_by_cart_session_id(cart_session_id or "")
    if not row:
        return JSONResponse({"error": "session_required"}, status_code=401)
    payment_id = str(row.get("mp_payment_id") or "")
    if not payment_id:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        refreshed = await refresh_pix_payment_status(payment_id, settings=settings)
    except MercadoPagoError as exc:
        return JSONResponse(
            {
                "error": exc.code or "mp_http_error",
                "message": str(exc),
                "status_code": exc.status_code,
            },
            status_code=502 if (exc.status_code or 500) >= 500 else 400,
        )
    return JSONResponse(
        {
            "paymentId": refreshed.get("payment_id"),
            "id": refreshed.get("payment_id"),
            "status": refreshed.get("status"),
            "settlement_status": row.get("settlement_status"),
            "paid_at": (
                row.get("paid_at").isoformat()
                if row.get("paid_at")
                else None
            ),
        }
    )


@router.get("/{payment_id}/status")
async def pix_payment_status_unbound(payment_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": "session_required", "payment_id": payment_id},
        status_code=401,
    )
