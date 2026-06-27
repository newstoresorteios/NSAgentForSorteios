from __future__ import annotations
from typing import Any
from .config import get_settings
from .db import get_conn


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or None


def format_cents_to_brl(cents: int | None) -> str:
    value = max(0, int(cents or 0))
    reais = value // 100
    centavos = value % 100
    reais_str = f"{reais:,}".replace(",", ".")
    return f"R$ {reais_str},{centavos:02d}"


def _lookup_user_by_phone(cur: Any, normalized: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT id, name, email, phone, coupon_value_cents
        FROM public.users
        WHERE regexp_replace(coalesce(phone, ''), '\\D', '', 'g') LIKE %(phone_like)s
        ORDER BY id DESC
        LIMIT 1
        """,
        {"phone_like": f"%{normalized[-9:]}"},
    )
    row = cur.fetchone()
    return row if row else None


def find_coupon_balance_by_phone(phone: str | None) -> dict[str, Any]:
    """Look up coupon balance in cents for a user matched by phone."""
    settings = get_settings()
    normalized = normalize_phone(phone)
    if not settings.database_url:
        return {"found": False, "error": "database_not_configured"}
    if not normalized:
        return {"found": False, "error": "phone_missing"}

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                row = _lookup_user_by_phone(cur, normalized)
                if not row:
                    return {"found": False}

                cents = row.get("coupon_value_cents")
                return {
                    "found": True,
                    "user_id": row.get("id"),
                    "name": row.get("name"),
                    "coupon_value_cents": int(cents) if cents is not None else 0,
                    "balance_brl": format_cents_to_brl(cents),
                }
    except Exception as exc:
        return {"found": False, "lookup_error": str(exc)[:180]}


def find_customer_profile_by_phone(phone: str | None) -> dict[str, Any]:
    """Return minimal, non-sensitive customer context by phone.

    This intentionally avoids exposing financial/account details. Add additional
    permitted queries only after reviewing consent, identity verification, and the
    applicable rules for your business domain.
    """
    settings = get_settings()
    normalized = normalize_phone(phone)
    if not settings.database_url or not normalized:
        return {"found": False}

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                row = _lookup_user_by_phone(cur, normalized)
                if not row:
                    return {"found": False}
                return {
                    "found": True,
                    "user_id": row.get("id"),
                    "name": row.get("name"),
                    "email_present": bool(row.get("email")),
                    "phone_present": bool(row.get("phone")),
                }
    except Exception as exc:
        return {"found": False, "lookup_error": str(exc)[:180]}
