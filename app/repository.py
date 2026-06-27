from __future__ import annotations

import re
from typing import Any

from .config import get_settings
from .db import get_conn


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or None


def phones_match(stored: str | None, incoming: str | None) -> bool:
    stored_norm = normalize_phone(stored)
    incoming_norm = normalize_phone(incoming)
    if not stored_norm or not incoming_norm:
        return False
    if len(stored_norm) < 9 or len(incoming_norm) < 9:
        return False
    return stored_norm[-9:] == incoming_norm[-9:]


def extract_phone_candidates(text: str | None) -> list[str]:
    if not text:
        return []
    candidates: list[str] = []
    for match in re.findall(r"\d[\d\s().-]{8,}\d", text):
        normalized = normalize_phone(match)
        if normalized and len(normalized) >= 10:
            candidates.append(normalized)
    return candidates


def detect_third_party_account_inquiry(text: str | None, sender_phone: str | None) -> bool:
    normalized_text = (text or "").lower()
    blocked_phrases = (
        "saldo do ",
        "saldo de ",
        "saldo da ",
        "cupom do ",
        "cupom de ",
        "telefone de ",
        "telefone do ",
        "outra pessoa",
        "outro usuario",
        "outro usuário",
        "cpf de ",
        "cpf do ",
    )
    if any(phrase in normalized_text for phrase in blocked_phrases):
        return True

    sender_norm = normalize_phone(sender_phone)
    for candidate in extract_phone_candidates(text):
        if sender_norm and candidate[-9:] != sender_norm[-9:]:
            return True
    return False


def format_cents_to_brl(cents: int | None) -> str:
    value = max(0, int(cents or 0))
    reais = value // 100
    centavos = value % 100
    reais_str = f"{reais:,}".replace(",", ".")
    return f"R$ {reais_str},{centavos:02d}"


def format_payment_numbers(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        return ", ".join(cleaned) if cleaned else None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    return str(raw)


def _format_participated_at(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _draw_label(draw_id: Any, title: Any) -> str:
    if title:
        return str(title)
    if draw_id is not None:
        return f"Sorteio #{draw_id}"
    return "Sorteio"


def _lookup_user_by_phone(cur: Any, normalized: str) -> dict[str, Any] | None:
    phone_digits = normalized
    phone_with_country = phone_digits if phone_digits.startswith("55") else f"55{phone_digits}"
    suffix = normalized[-9:]

    queries = (
        """
        SELECT id, name, email, phone, coupon_value_cents, coupon_code
        FROM public.users
        WHERE nullif(regexp_replace(coalesce(phone, ''), '\\D', '', 'g'), '') IS NOT NULL
          AND (
            regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(exact)s
            OR regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(with_country)s
            OR regexp_replace(coalesce(phone, ''), '\\D', '', 'g') LIKE %(suffix_like)s
          )
        ORDER BY
          CASE
            WHEN regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(exact)s THEN 0
            WHEN regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(with_country)s THEN 1
            ELSE 2
          END,
          length(regexp_replace(coalesce(phone, ''), '\\D', '', 'g')) DESC,
          id DESC
        LIMIT 1
        """,
        """
        SELECT id, name, email, phone, coupon_value_cents
        FROM public.users
        WHERE nullif(regexp_replace(coalesce(phone, ''), '\\D', '', 'g'), '') IS NOT NULL
          AND (
            regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(exact)s
            OR regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(with_country)s
            OR regexp_replace(coalesce(phone, ''), '\\D', '', 'g') LIKE %(suffix_like)s
          )
        ORDER BY
          CASE
            WHEN regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(exact)s THEN 0
            WHEN regexp_replace(coalesce(phone, ''), '\\D', '', 'g') = %(with_country)s THEN 1
            ELSE 2
          END,
          length(regexp_replace(coalesce(phone, ''), '\\D', '', 'g')) DESC,
          id DESC
        LIMIT 1
        """,
    )
    params = {
        "exact": phone_digits,
        "with_country": phone_with_country,
        "suffix_like": f"%{suffix}",
    }
    for sql in queries:
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row:
                return dict(row)
        except Exception:
            continue
    return None


def _resolve_account(phone: str | None, message_text: str | None) -> dict[str, Any]:
    settings = get_settings()
    normalized = normalize_phone(phone)
    if not settings.database_url:
        return {"found": False, "error": "database_not_configured"}
    if not normalized:
        return {"found": False, "error": "phone_missing"}
    if detect_third_party_account_inquiry(message_text, phone):
        return {"found": False, "error": "third_party_inquiry"}

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                row = _lookup_user_by_phone(cur, normalized)
                if not row:
                    return {"found": False}

                stored_phone = row.get("phone")
                if not normalize_phone(stored_phone):
                    return {"found": False, "error": "phone_not_registered"}

                if not phones_match(stored_phone, phone):
                    return {"found": False, "error": "third_party_inquiry"}

                cents = row.get("coupon_value_cents")
                return {
                    "found": True,
                    "user_id": row.get("id"),
                    "name": row.get("name"),
                    "phone": stored_phone,
                    "coupon_code": row.get("coupon_code"),
                    "coupon_value_cents": int(cents) if cents is not None else 0,
                    "balance_brl": format_cents_to_brl(cents),
                }
    except Exception as exc:
        return {"found": False, "lookup_error": str(exc)[:180]}


def find_coupon_balance_by_phone(phone: str | None, message_text: str | None = None) -> dict[str, Any]:
    return _resolve_account(phone, message_text)


def find_user_coupon_code(phone: str | None, message_text: str | None = None) -> dict[str, Any]:
    return _resolve_account(phone, message_text)


def find_customer_profile_by_phone(phone: str | None) -> dict[str, Any]:
    account = _resolve_account(phone, None)
    if not account.get("found"):
        return {"found": False, "lookup_error": account.get("lookup_error")} if account.get("lookup_error") else {"found": False}

    return {
        "found": True,
        "user_id": account.get("user_id"),
        "name": account.get("name"),
        "email_present": True,
        "phone_present": True,
    }


def find_current_raffle() -> dict[str, Any]:
    settings = get_settings()
    if not settings.database_url:
        return {"found": False, "error": "database_not_configured"}

    queries = (
        """
        SELECT id, title, prize_name, status, winning_number, quota_price_cents
        FROM public.raffles
        WHERE lower(coalesce(status, '')) IN ('open', 'active', 'available', 'aberto')
        ORDER BY id DESC
        LIMIT 1
        """,
        """
        SELECT id, title, prize_name, status, winning_number, quota_price_cents
        FROM public.raffles
        WHERE is_current = true
        ORDER BY id DESC
        LIMIT 1
        """,
        """
        SELECT id, title, prize_name, status, winning_number, quota_price_cents
        FROM public.raffles
        ORDER BY id DESC
        LIMIT 1
        """,
    )

    last_error: str | None = None
    for sql in queries:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    row = cur.fetchone()
                    if row:
                        quota = row.get("quota_price_cents")
                        return {
                            "found": True,
                            "id": row.get("id"),
                            "title": row.get("title"),
                            "prize_name": row.get("prize_name"),
                            "status": row.get("status"),
                            "winning_number": row.get("winning_number"),
                            "quota_price_brl": format_cents_to_brl(quota) if quota is not None else None,
                        }
        except Exception as exc:
            last_error = str(exc)[:180]
            continue

    if last_error:
        return {"found": False, "lookup_error": last_error}
    return {"found": False}


def find_last_payment_participation(user_id: int) -> dict[str, Any]:
    settings = get_settings()
    if not settings.database_url:
        return {"found": False, "error": "database_not_configured"}

    queries = (
        """
        SELECT
          p.id,
          p.user_id,
          coalesce(p.created_at, p.paid_at) AS participated_at,
          p.draw_id,
          p.numbers,
          p.amount_cents,
          coalesce(d.title, r.title) AS raffle_title,
          coalesce(d.winning_number, r.winning_number) AS winning_number
        FROM public.payments p
        LEFT JOIN public.draws d ON d.id = p.draw_id
        LEFT JOIN public.raffles r ON r.id = p.draw_id
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY coalesce(p.created_at, p.paid_at) DESC NULLS LAST, p.id DESC
        LIMIT 1
        """,
        """
        SELECT
          p.id,
          p.user_id,
          coalesce(p.paid_at, p.created_at) AS participated_at,
          coalesce(p.draw_id, p.raffle_id, p.sorteio_id) AS draw_id,
          p.numbers,
          p.amount_cents,
          r.title AS raffle_title,
          r.winning_number
        FROM public.payments p
        LEFT JOIN public.raffles r ON r.id = coalesce(p.raffle_id, p.sorteio_id, p.draw_id)
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY coalesce(p.paid_at, p.created_at) DESC NULLS LAST, p.id DESC
        LIMIT 1
        """,
        """
        SELECT
          p.id,
          p.user_id,
          p.created_at AS participated_at,
          p.draw_id,
          p.numbers,
          p.amount_cents,
          NULL::text AS raffle_title,
          NULL::text AS winning_number
        FROM public.payments p
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY p.created_at DESC NULLS LAST, p.id DESC
        LIMIT 1
        """,
    )

    last_error: str | None = None
    for sql in queries:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"user_id": user_id})
                    row = cur.fetchone()
                    if row:
                        participated_at = _format_participated_at(row.get("participated_at"))
                        draw_id = row.get("draw_id")
                        title = row.get("raffle_title")
                        numbers = format_payment_numbers(row.get("numbers"))
                        return {
                            "found": True,
                            "payment_id": row.get("id"),
                            "draw_id": draw_id,
                            "participated_at": participated_at,
                            "raffle_title": title or _draw_label(draw_id, None),
                            "numbers": numbers,
                            "amount_brl": format_cents_to_brl(row.get("amount_cents")),
                            "winning_number": row.get("winning_number"),
                        }
        except Exception as exc:
            last_error = str(exc)[:180]
            continue

    if last_error:
        return {"found": False, "lookup_error": last_error}
    return {"found": False}


def find_user_raffle_participation(user_id: int) -> dict[str, Any]:
    settings = get_settings()
    if not settings.database_url:
        return {"found": False, "error": "database_not_configured"}

    queries = (
        """
        SELECT
          p.draw_id,
          p.numbers,
          p.amount_cents,
          coalesce(p.created_at, p.paid_at) AS participated_at,
          coalesce(d.title, r.title) AS title,
          coalesce(d.winning_number, r.winning_number) AS winning_number,
          coalesce(d.status, r.status) AS status
        FROM public.payments p
        LEFT JOIN public.draws d ON d.id = p.draw_id
        LEFT JOIN public.raffles r ON r.id = p.draw_id
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY coalesce(p.created_at, p.paid_at) DESC NULLS LAST, p.id DESC
        LIMIT 50
        """,
        """
        SELECT
          coalesce(p.draw_id, p.raffle_id, p.sorteio_id) AS draw_id,
          p.numbers,
          p.amount_cents,
          coalesce(p.paid_at, p.created_at) AS participated_at,
          r.title,
          r.winning_number,
          r.status
        FROM public.payments p
        LEFT JOIN public.raffles r ON r.id = coalesce(p.draw_id, p.raffle_id, p.sorteio_id)
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY coalesce(p.paid_at, p.created_at) DESC NULLS LAST, p.id DESC
        LIMIT 50
        """,
        """
        SELECT
          p.draw_id,
          p.numbers,
          p.amount_cents,
          p.created_at AS participated_at,
          NULL::text AS title,
          NULL::text AS winning_number,
          NULL::text AS status
        FROM public.payments p
        WHERE p.user_id = %(user_id)s
          AND lower(coalesce(p.status, '')) = 'approved'
        ORDER BY p.created_at DESC NULLS LAST, p.id DESC
        LIMIT 50
        """,
    )

    last_error: str | None = None
    for sql in queries:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"user_id": user_id})
                    rows = cur.fetchall()
                    if rows:
                        grouped: dict[Any, dict[str, Any]] = {}
                        for row in rows:
                            draw_id = row.get("draw_id")
                            if draw_id is None:
                                continue
                            bucket = grouped.get(draw_id)
                            participated_at = _format_participated_at(row.get("participated_at"))
                            numbers = format_payment_numbers(row.get("numbers"))
                            amount_cents = int(row.get("amount_cents") or 0)
                            if bucket is None:
                                grouped[draw_id] = {
                                    "draw_id": draw_id,
                                    "title": row.get("title") or _draw_label(draw_id, None),
                                    "numbers_parts": [numbers] if numbers else [],
                                    "winning_number": row.get("winning_number"),
                                    "winner_name": None,
                                    "status": row.get("status"),
                                    "participated_at": participated_at,
                                    "amount_cents": amount_cents,
                                }
                                continue

                            if numbers and numbers not in bucket["numbers_parts"]:
                                bucket["numbers_parts"].append(numbers)
                            bucket["amount_cents"] += amount_cents
                            if participated_at and (
                                not bucket["participated_at"] or participated_at > bucket["participated_at"]
                            ):
                                bucket["participated_at"] = participated_at
                            if row.get("title"):
                                bucket["title"] = row.get("title")
                            if row.get("winning_number"):
                                bucket["winning_number"] = row.get("winning_number")
                            if row.get("status"):
                                bucket["status"] = row.get("status")

                        items = []
                        for draw_id, bucket in grouped.items():
                            numbers = " | ".join(bucket["numbers_parts"]) if bucket["numbers_parts"] else None
                            items.append(
                                {
                                    "draw_id": draw_id,
                                    "title": bucket["title"],
                                    "numbers": numbers,
                                    "winning_number": bucket["winning_number"],
                                    "winner_name": bucket["winner_name"],
                                    "status": bucket["status"],
                                    "participated_at": bucket["participated_at"],
                                    "amount_brl": format_cents_to_brl(bucket["amount_cents"]),
                                }
                            )
                        items.sort(key=lambda item: item.get("participated_at") or "", reverse=True)
                        return {"found": True, "items": items[:5]}
        except Exception as exc:
            last_error = str(exc)[:180]
            continue

    if last_error:
        return {"found": False, "lookup_error": last_error}
    return {"found": False}
