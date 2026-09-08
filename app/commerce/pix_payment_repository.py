"""Persistence for Mercado Pago PIX payments created by the agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_conn, get_returning_id, to_jsonb


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return dict(row)


def upsert_pix_payment_created(
    *,
    mp_payment_id: str,
    status: str,
    amount_cents: int,
    description: str | None = None,
    payer_email: str | None = None,
    qr_code: str | None = None,
    qr_code_base64: str | None = None,
    external_reference: str | None = None,
    date_of_expiration: datetime | None = None,
    expires_at: datetime | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
    channel: str | None = None,
    cart_session_id: str | None = None,
    checkout_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    raw_create: dict[str, Any] | None = None,
    currency: str = "BRL",
) -> int | None:
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_pix_payments (
                    mp_payment_id, status, amount_cents, currency, description,
                    payer_email, qr_code, qr_code_base64, external_reference,
                    date_of_expiration, expires_at,
                    conversation_id, sender_key, sender_phone, channel,
                    cart_session_id, checkout_snapshot, metadata, raw_create,
                    created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (mp_payment_id) DO UPDATE SET
                    status = public.ai_pix_payments.status,
                    qr_code = COALESCE(EXCLUDED.qr_code, public.ai_pix_payments.qr_code),
                    qr_code_base64 = COALESCE(
                        EXCLUDED.qr_code_base64,
                        public.ai_pix_payments.qr_code_base64
                    ),
                    description = COALESCE(
                        EXCLUDED.description,
                        public.ai_pix_payments.description
                    ),
                    payer_email = COALESCE(
                        EXCLUDED.payer_email,
                        public.ai_pix_payments.payer_email
                    ),
                    external_reference = COALESCE(
                        EXCLUDED.external_reference,
                        public.ai_pix_payments.external_reference
                    ),
                    date_of_expiration = COALESCE(
                        EXCLUDED.date_of_expiration,
                        public.ai_pix_payments.date_of_expiration
                    ),
                    expires_at = COALESCE(
                        EXCLUDED.expires_at,
                        public.ai_pix_payments.expires_at
                    ),
                    conversation_id = COALESCE(
                        EXCLUDED.conversation_id,
                        public.ai_pix_payments.conversation_id
                    ),
                    sender_key = COALESCE(
                        EXCLUDED.sender_key,
                        public.ai_pix_payments.sender_key
                    ),
                    sender_phone = COALESCE(
                        EXCLUDED.sender_phone,
                        public.ai_pix_payments.sender_phone
                    ),
                    channel = COALESCE(EXCLUDED.channel, public.ai_pix_payments.channel),
                    cart_session_id = COALESCE(
                        EXCLUDED.cart_session_id,
                        public.ai_pix_payments.cart_session_id
                    ),
                    checkout_snapshot = public.ai_pix_payments.checkout_snapshot,
                    metadata = CASE
                        WHEN EXCLUDED.metadata = '{}'::jsonb
                        THEN public.ai_pix_payments.metadata
                        ELSE EXCLUDED.metadata
                    END,
                    raw_create = CASE
                        WHEN EXCLUDED.raw_create = '{}'::jsonb
                        THEN public.ai_pix_payments.raw_create
                        ELSE EXCLUDED.raw_create
                    END,
                    updated_at = CASE WHEN public.ai_pix_payments.settlement_status = 'processing'
                        THEN public.ai_pix_payments.updated_at ELSE EXCLUDED.updated_at END
                WHERE public.ai_pix_payments.checkout_snapshot = EXCLUDED.checkout_snapshot
                  AND public.ai_pix_payments.amount_cents = EXCLUDED.amount_cents
                  AND public.ai_pix_payments.currency = EXCLUDED.currency
                  AND public.ai_pix_payments.cart_session_id IS NOT DISTINCT FROM EXCLUDED.cart_session_id
                  AND public.ai_pix_payments.sender_key IS NOT DISTINCT FROM EXCLUDED.sender_key
                  AND public.ai_pix_payments.conversation_id IS NOT DISTINCT FROM EXCLUDED.conversation_id
                RETURNING id
                """,
                (
                    str(mp_payment_id),
                    str(status or "pending"),
                    int(amount_cents),
                    currency or "BRL",
                    description,
                    payer_email,
                    qr_code,
                    qr_code_base64,
                    external_reference,
                    date_of_expiration,
                    expires_at,
                    conversation_id,
                    sender_key,
                    sender_phone,
                    channel,
                    cart_session_id,
                    to_jsonb(checkout_snapshot or {}),
                    to_jsonb(metadata or {}),
                    to_jsonb(raw_create or {}),
                    now,
                    now,
                ),
            )
            saved = cur.fetchone()
            if not saved:
                raise ValueError("pix_payment_identity_conflict")
            return get_returning_id(saved)


def get_pix_payment_by_mp_id(mp_payment_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_pix_payments
                WHERE mp_payment_id = %s
                LIMIT 1
                """,
                (str(mp_payment_id),),
            )
            return _row_to_dict(cur.fetchone())


def apply_mp_status_update(
    *,
    mp_payment_id: str,
    status: str,
    raw_last_status: dict[str, Any] | None = None,
    paid_at: datetime | None = None,
    mark_settlement_pending_on_approved: bool = True,
) -> dict[str, Any] | None:
    """Update status from MP poll/webhook. Returns updated row or None if missing."""
    now = _now()
    status_norm = str(status or "").strip().lower()
    is_approved = status_norm == "approved"
    effective_paid_at = paid_at if is_approved else None
    if is_approved and effective_paid_at is None:
        effective_paid_at = now

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET status = %s,
                    paid_at = CASE
                        WHEN %s THEN COALESCE(paid_at, %s)
                        ELSE paid_at
                    END,
                    settlement_status = CASE
                        WHEN %s
                             AND %s
                             AND settlement_status = 'none'
                        THEN 'pending'
                        ELSE settlement_status
                    END,
                    last_webhook_at = %s,
                    raw_last_status = %s,
                    updated_at = CASE WHEN settlement_status = 'processing' THEN updated_at ELSE %s END
                WHERE mp_payment_id = %s
                RETURNING *
                """,
                (
                    status_norm or "pending",
                    is_approved,
                    effective_paid_at,
                    is_approved,
                    mark_settlement_pending_on_approved,
                    now,
                    to_jsonb(raw_last_status or {}),
                    now,
                    str(mp_payment_id),
                ),
            )
            return _row_to_dict(cur.fetchone())


def claim_pix_settlement(mp_payment_id: str) -> dict[str, Any] | None:
    """Atomically move pending → processing. Returns row if claim succeeded."""
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET settlement_status = 'processing',
                    settlement_error = NULL,
                    updated_at = %s
                WHERE mp_payment_id = %s
                  AND lower(status) = 'approved'
                  AND settlement_status = 'pending'
                RETURNING *
                """,
                (now, str(mp_payment_id)),
            )
            return _row_to_dict(cur.fetchone())


def mark_pix_settlement(
    mp_payment_id: str,
    *,
    settlement_status: str,
    tray_order_id: str | None = None,
    settlement_error: str | None = None,
) -> dict[str, Any] | None:
    now = _now()
    settled = settlement_status in {"completed", "skipped"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET settlement_status = %s,
                    tray_order_id = COALESCE(%s, tray_order_id),
                    settlement_error = %s,
                    settled_at = CASE WHEN %s THEN COALESCE(settled_at, %s) ELSE settled_at END,
                    updated_at = %s
                WHERE mp_payment_id = %s
                RETURNING *
                """,
                (
                    settlement_status,
                    tray_order_id,
                    settlement_error,
                    settled,
                    now,
                    now,
                    str(mp_payment_id),
                ),
            )
            return _row_to_dict(cur.fetchone())


_NON_RETRYABLE_SETTLEMENT = frozenset(
    {
        "amount_mismatch",
        "checkout_snapshot_incomplete",
        "stored_amount_missing",
        "expected_amount_missing",
        "mp_amount_missing",
        "pix_not_approved",
    }
)


def is_retryable_settlement_error(error: str | None) -> bool:
    text = str(error or "").strip()
    if text in _NON_RETRYABLE_SETTLEMENT:
        return False
    if not text:
        return True
    return text.startswith("tray_")


def get_pix_payment_by_tray_order_id(tray_order_id: str) -> dict[str, Any] | None:
    oid = str(tray_order_id or "").strip()
    if not oid:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_pix_payments
                WHERE tray_order_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (oid,),
            )
            return _row_to_dict(cur.fetchone())


def get_pix_payment_by_cart_session_id(cart_session_id: str) -> dict[str, Any] | None:
    session_id = str(cart_session_id or "").strip()
    if not session_id:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_pix_payments
                WHERE cart_session_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id,),
            )
            return _row_to_dict(cur.fetchone())


def list_retryable_pix_settlements(*, limit: int = 10) -> list[dict[str, Any]]:
    capped = min(max(int(limit), 1), 50)
    stale_before = _now() - timedelta(seconds=600)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM public.ai_pix_payments
                WHERE lower(status) = 'approved'
                  AND tray_order_id IS NULL
                  AND (
                    settlement_status = 'pending'
                    OR (settlement_status = 'processing' AND updated_at <= %s)
                    OR (settlement_status = 'failed' AND
                        (COALESCE(settlement_error, '') = '' OR left(settlement_error, 5) = 'tray_'))
                  )
                ORDER BY updated_at ASC
                LIMIT %s
                """,
                (stale_before, capped),
            )
            rows = cur.fetchall() or []
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row)
        if not item:
            continue
        status = str(item.get("settlement_status") or "")
        if status == "failed" and not is_retryable_settlement_error(
            item.get("settlement_error")
        ):
            continue
        if status == "processing":
            updated = item.get("updated_at")
            stamp = updated.timestamp() if hasattr(updated, "timestamp") else 0
            if stamp > stale_before.timestamp():
                continue
        result.append(item)
    return result


def requeue_pix_settlement(mp_payment_id: str) -> dict[str, Any] | None:
    """Move failed/stale processing back to pending when PIX is approved and Tray order is missing."""
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_pix_payments
                SET settlement_status = 'pending',
                    settlement_error = NULL,
                    updated_at = %s
                WHERE mp_payment_id = %s
                  AND lower(status) = 'approved'
                  AND tray_order_id IS NULL
                  AND (
                    (settlement_status = 'processing' AND updated_at <= %s)
                    OR (settlement_status = 'failed' AND
                        (COALESCE(settlement_error, '') = '' OR left(settlement_error, 5) = 'tray_'))
                  )
                RETURNING *
                """,
                (now, str(mp_payment_id), now - timedelta(seconds=600)),
            )
            return _row_to_dict(cur.fetchone())
