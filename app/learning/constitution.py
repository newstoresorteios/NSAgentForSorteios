"""Code-level constitution for auto-activated learning deltas."""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings


_MONEY_RE = re.compile(r"R\$|\b\d{2,}\s*reais\b", flags=re.IGNORECASE)
_URL_RE = re.compile(r"https?://|\bwww\.", flags=re.IGNORECASE)
_TRADE_BUY_RE = re.compile(
    r"(avalia(mos)?|trocamos|compramos).{0,60}"
    r"(seminovo|usado|particulares)|"
    r"avalia,\s*troca e compra",
    flags=re.IGNORECASE,
)
_TRADE_DENIAL_RE = re.compile(
    r"n[aã]o avalia(mos)?|n[aã]o compra(mos)?|n[aã]o troca(mos)?",
    flags=re.IGNORECASE,
)
_SKIP_TRAY_RE = re.compile(
    r"(n[aã]o consulte|n[aã]o chame|pular|skip|ignore|sem consultar).{0,40}tray|"
    r"n[aã]o (use|usar|chamar) (a )?tray",
    flags=re.IGNORECASE,
)


def check_instruction_delta(
    text: str,
    *,
    max_chars: int | None = None,
) -> tuple[bool, str | None]:
    """Return (ok, rejection_reason)."""
    settings = get_settings()
    limit = int(
        max_chars
        if max_chars is not None
        else getattr(settings, "agent_learning_max_instruction_chars", 800) or 800
    )
    instruction = (text or "").strip()
    if not instruction:
        return False, "empty_instruction"
    if len(instruction) > limit:
        return False, "instruction_too_long"
    if _MONEY_RE.search(instruction):
        return False, "price_claim"
    if _URL_RE.search(instruction):
        return False, "url_claim"
    if _TRADE_BUY_RE.search(instruction) and not _TRADE_DENIAL_RE.search(instruction):
        return False, "trade_in_policy_rewrite"
    if _SKIP_TRAY_RE.search(instruction):
        return False, "skip_tray"
    return True, None


def constitution_metadata(reason: str | None) -> dict[str, Any]:
    return {"constitution_rejected": True, "constitution_reason": reason}
