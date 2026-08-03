from __future__ import annotations

import re
from typing import Any

from .commerce_context import CommerceConversationState
from .models import IncomingMessage


def normalize_digits(value: str | None) -> str | None:
    digits = re.sub(r"\D+", "", value or "")
    return digits or None


def normalize_email(value: str | None) -> str | None:
    email = (value or "").strip().casefold()
    return email or None


def person_key_from_parts(
    *,
    cpf: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> str | None:
    cpf_digits = normalize_digits(cpf)
    if cpf_digits and len(cpf_digits) in {11, 14}:
        return f"cpf:{cpf_digits}"
    phone_digits = normalize_digits(phone)
    if phone_digits and len(phone_digits) >= 10:
        return f"phone:{phone_digits}"
    email_norm = normalize_email(email)
    if email_norm:
        return f"email:{email_norm}"
    return None


def identities_from_message_and_state(
    message: IncomingMessage | None,
    state: CommerceConversationState | dict[str, Any] | None,
) -> list[tuple[str, str, str | None]]:
    """Return (identity_type, identity_value, channel) tuples worth linking."""
    payload = (
        state.model_dump(mode="json")
        if isinstance(state, CommerceConversationState)
        else (state if isinstance(state, dict) else {})
    )
    draft = payload.get("checkout_draft") if isinstance(payload, dict) else {}
    customer = draft.get("customer") if isinstance(draft, dict) else {}
    if not isinstance(customer, dict):
        customer = {}

    channel = getattr(message, "channel", None) if message else None
    rows: list[tuple[str, str, str | None]] = []

    def add(kind: str, value: str | None) -> None:
        if value:
            rows.append((kind, value, channel))

    phone = normalize_digits(
        getattr(message, "sender_phone", None) if message else None
    ) or normalize_digits(customer.get("phone"))
    cpf = normalize_digits(customer.get("cpf"))
    email = normalize_email(customer.get("email"))
    sender_key = (getattr(message, "sender_key", None) or "").strip() or None

    add("phone", phone)
    add("cpf", cpf)
    add("email", email)
    add("sender_key", sender_key)
    return rows


def upsert_customer_identity_links(
    message: IncomingMessage | None,
    state: CommerceConversationState | dict[str, Any] | None,
) -> None:
    """Persist cross-channel identity aliases when strong customer signals exist."""
    from .db import ensure_tables, get_conn, get_settings

    settings = get_settings()
    if not settings.database_url:
        return

    identities = identities_from_message_and_state(message, state)
    if not identities:
        return

    by_type = {kind: value for kind, value, _channel in identities}
    person_key = person_key_from_parts(
        cpf=by_type.get("cpf"),
        phone=by_type.get("phone"),
        email=by_type.get("email"),
    )
    if not person_key:
        # Without CPF/phone/email we cannot safely merge channels.
        return
    if len(identities) < 2 and not by_type.get("cpf"):
        # Keep a phone-only seed so later CPF can attach to the same person.
        if not by_type.get("phone"):
            return

    ensure_tables()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT person_key
                    FROM public.ai_customer_identity_links
                    WHERE (identity_type, identity_value) IN (
                      SELECT * FROM unnest(%(types)s::text[], %(values)s::text[])
                        AS t(identity_type, identity_value)
                    )
                    """,
                    {
                        "types": [item[0] for item in identities],
                        "values": [item[1] for item in identities],
                    },
                )
                existing_keys = {
                    str(row["person_key"])
                    for row in (cur.fetchall() or [])
                    if row.get("person_key")
                }
                preferred = person_key
                for key in sorted(existing_keys):
                    if key.startswith("cpf:"):
                        preferred = key
                        break
                if existing_keys:
                    cur.execute(
                        """
                        UPDATE public.ai_customer_identity_links
                        SET person_key = %(preferred)s, updated_at = now()
                        WHERE person_key = ANY(%(keys)s)
                        """,
                        {"preferred": preferred, "keys": list(existing_keys | {person_key})},
                    )
                for identity_type, identity_value, channel in identities:
                    cur.execute(
                        """
                        INSERT INTO public.ai_customer_identity_links
                          (person_key, identity_type, identity_value, channel, updated_at)
                        VALUES
                          (%(person_key)s, %(identity_type)s, %(identity_value)s, %(channel)s, now())
                        ON CONFLICT (identity_type, identity_value) DO UPDATE
                        SET
                          person_key = EXCLUDED.person_key,
                          channel = COALESCE(EXCLUDED.channel, public.ai_customer_identity_links.channel),
                          updated_at = now()
                        """,
                        {
                            "person_key": preferred,
                            "identity_type": identity_type,
                            "identity_value": identity_value,
                            "channel": channel,
                        },
                    )
    except Exception as exc:
        print("[sales.identity] upsert_failed", {"error_type": type(exc).__name__})


def resolve_linked_identity_candidates(
    *,
    sender_key: str | None,
    sender_phone: str | None,
    cpf: str | None = None,
    email: str | None = None,
) -> list[tuple[str | None, str | None]]:
    """Return extra (sender_key, phone) pairs linked to the same person."""
    from .db import ensure_tables, get_conn, get_settings

    settings = get_settings()
    if not settings.database_url:
        return []

    probes: list[tuple[str, str]] = []
    phone = normalize_digits(sender_phone)
    cpf_digits = normalize_digits(cpf)
    email_norm = normalize_email(email)
    key = (sender_key or "").strip() or None
    if key:
        probes.append(("sender_key", key))
    if phone:
        probes.append(("phone", phone))
    if cpf_digits:
        probes.append(("cpf", cpf_digits))
    if email_norm:
        probes.append(("email", email_norm))
    if not probes:
        return []

    ensure_tables()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH seed AS (
                      SELECT DISTINCT person_key
                      FROM public.ai_customer_identity_links
                      WHERE (identity_type, identity_value) IN (
                        SELECT * FROM unnest(%(types)s::text[], %(values)s::text[])
                          AS t(identity_type, identity_value)
                      )
                    )
                    SELECT identity_type, identity_value
                    FROM public.ai_customer_identity_links
                    WHERE person_key IN (SELECT person_key FROM seed)
                    """,
                    {
                        "types": [item[0] for item in probes],
                        "values": [item[1] for item in probes],
                    },
                )
                rows = cur.fetchall() or []
    except Exception as exc:
        print("[sales.identity] resolve_failed", {"error_type": type(exc).__name__})
        return []

    linked_keys: set[str] = set()
    linked_phones: set[str] = set()
    for row in rows:
        kind = str(row.get("identity_type") or "")
        value = str(row.get("identity_value") or "")
        if kind == "sender_key" and value:
            linked_keys.add(value)
        elif kind == "phone" and value:
            linked_phones.add(value)

    candidates: list[tuple[str | None, str | None]] = []
    for linked_key in sorted(linked_keys):
        if linked_key != key:
            candidates.append((linked_key, None))
    for linked_phone in sorted(linked_phones):
        if linked_phone != phone:
            candidates.append((None, linked_phone))
    return candidates
