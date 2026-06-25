from __future__ import annotations
from typing import Any
from .config import get_settings
from .db import get_conn


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or None


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
                # Best-effort lookup for common columns. Keep response minimal.
                cur.execute(
                    """
                    SELECT id, name, email, phone
                    FROM public.users
                    WHERE regexp_replace(coalesce(phone, ''), '\\D', '', 'g') LIKE %(phone_like)s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    {"phone_like": f"%{normalized[-9:]}"},
                )
                row = cur.fetchone()
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
