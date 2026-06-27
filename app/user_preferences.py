from __future__ import annotations

import re
from typing import Any

from .db import get_conn


def extract_first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    first = full_name.strip().split()[0]
    if len(first) < 2:
        return None
    return first[:1].upper() + first[1:].lower() if first.islower() or first.isupper() else first


def detect_preferred_name_update(text: str | None) -> str | None:
    if not text:
        return None

    patterns = (
        r"(?:me chame|me cham|pode me chamar|quero ser chamad[oa]|prefiro ser chamad[oa]) de\s+(.+?)[!.?\s]*$",
        r"(?:prefiro o nome|pode usar o nome)\s+(.+?)[!.?\s]*$",
    )
    normalized = text.strip()
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            preferred = match.group(1).strip(" \"'")
            if preferred:
                return preferred[:80]
    return None


def get_user_preferences(user_id: int) -> dict[str, Any]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, preferred_name, ask_preferred_name, last_preferred_name_prompt_at
                    FROM public.ai_user_preferences
                    WHERE user_id = %(user_id)s
                    LIMIT 1
                    """,
                    {"user_id": user_id},
                )
                row = cur.fetchone()
                if not row:
                    return {
                        "user_id": user_id,
                        "preferred_name": None,
                        "ask_preferred_name": True,
                        "exists": False,
                    }
                return {
                    "user_id": row.get("user_id"),
                    "preferred_name": row.get("preferred_name"),
                    "ask_preferred_name": bool(row.get("ask_preferred_name")),
                    "last_preferred_name_prompt_at": row.get("last_preferred_name_prompt_at"),
                    "exists": True,
                }
    except Exception:
        return {
            "user_id": user_id,
            "preferred_name": None,
            "ask_preferred_name": True,
            "exists": False,
        }


def save_preferred_name(user_id: int, preferred_name: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_user_preferences
                  (user_id, preferred_name, ask_preferred_name, updated_at)
                VALUES
                  (%(user_id)s, %(preferred_name)s, false, now())
                ON CONFLICT (user_id) DO UPDATE SET
                  preferred_name = EXCLUDED.preferred_name,
                  ask_preferred_name = false,
                  updated_at = now()
                """,
                {"user_id": user_id, "preferred_name": preferred_name},
            )


def mark_preferred_name_prompted(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_user_preferences
                  (user_id, ask_preferred_name, last_preferred_name_prompt_at, updated_at)
                VALUES
                  (%(user_id)s, true, now(), now())
                ON CONFLICT (user_id) DO UPDATE SET
                  last_preferred_name_prompt_at = now(),
                  updated_at = now()
                """,
                {"user_id": user_id},
            )


def resolve_display_name(account_name: str | None, preferences: dict[str, Any]) -> str | None:
    preferred = (preferences.get("preferred_name") or "").strip()
    if preferred:
        return preferred
    return extract_first_name(account_name)
