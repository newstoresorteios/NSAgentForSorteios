"""Avoid redundant replies — especially repeated greetings to the same person."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

GREETING_REPLY = "Olá! Como posso ajudar?"

# Short, distinct follow-ups when the primary Crono greeting was already used.
# Keep Crono identity in the first fallbacks so soft re-greets don't sound generic.
_FALLBACK_GREETING_VARIANTS = (
    "Oi! Sou o Crono da New Store Relógios. Em que posso te ajudar?",
    "Olá de novo! Sou o Crono — me conta o que você procura.",
    GREETING_REPLY,
    "Oi! Em que posso te ajudar?",
    "Olá! Me conta o que você procura.",
    "Oi! Pode falar, estou aqui.",
    "Olá! Como posso te ajudar agora?",
)

_GREETING_BODY_RE = re.compile(
    r"^\s*(ol[aá]|oi|bom dia|boa tarde|boa noite)[!.,\s]*"
    r"(como posso (te )?ajudar|em que posso (te )?ajudar|"
    r"me conta o que (você|voce) procura|pode falar|"
    r"estou aqui|tudo bem|eu sou o crono)[!.?\s]*.*$",
    flags=re.IGNORECASE,
)

_GREETING_LABEL_RE = re.compile(
    r"^\s*(saudação|saudacao)\s+padr[aã]o"
    r"(?:\s*\([^)]*\))?\s*:\s*",
    flags=re.IGNORECASE,
)

_FAREWELL_RE = re.compile(
    r"^\s*(at[eé]|tchau|obrigad[oa]|valeu|flw|falou|por hoje)\b",
    flags=re.IGNORECASE,
)


def sanitize_greeting_reply(text: str | None) -> str:
    """Strip instruction labels the model may echo from persona prose."""
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return ""
    cleaned = _GREETING_LABEL_RE.sub("", cleaned).strip()
    # Drop leftover meta prefixes like "Saudação oficial:"
    cleaned = re.sub(
        r"^\s*(saudação|saudacao)\s+(oficial|inicial)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def resolve_persona_greeting() -> str | None:
    """Primary greeting from ChatBo persona profile / active DB persona."""
    try:
        from .persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None and (runtime.greeting_text or "").strip():
            return sanitize_greeting_reply(runtime.greeting_text)
    except Exception:
        pass
    try:
        from .config import get_settings
        from .persona_knowledge_repository import (
            chatbo_persona_id,
            get_chatbo_persona_profile,
        )
        from .persona_repository import (
            DEFAULT_PERSONA_KEY,
            DEFAULT_TENANT_ID,
            get_active_persona,
        )

        settings = get_settings()
        if not bool(getattr(settings, "agent_db_persona_enabled", False)):
            return None
        tenant_id = str(
            getattr(settings, "agent_persona_tenant_id", DEFAULT_TENANT_ID)
            or DEFAULT_TENANT_ID
        )
        persona_key = str(
            getattr(settings, "agent_persona_key", DEFAULT_PERSONA_KEY)
            or DEFAULT_PERSONA_KEY
        )
        active = get_active_persona(tenant_id, persona_key)
        if active is None:
            return None
        chatbo_id = chatbo_persona_id(active.metadata)
        if chatbo_id:
            profile = get_chatbo_persona_profile(chatbo_id) or {}
            greeting = sanitize_greeting_reply(profile.get("greeting"))
            if greeting:
                return greeting
        # Fallback: first line in instructions that looks like a Crono greeting.
        for line in str(active.instructions or "").splitlines():
            cleaned = sanitize_greeting_reply(line.strip().strip('"').strip("'"))
            if "eu sou o crono" in cleaned.casefold() and len(cleaned) <= 220:
                return cleaned
    except Exception as exc:
        print("[greeting.persona.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
    return None


def greeting_variants() -> tuple[str, ...]:
    """Persona greeting first, then canned fallbacks (deduped)."""
    ordered: list[str] = []
    seen: set[str] = set()
    primary = resolve_persona_greeting()
    for candidate in (
        primary,
        *(_FALLBACK_GREETING_VARIANTS),
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        key = _fold(text)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return tuple(ordered) or _FALLBACK_GREETING_VARIANTS


def is_generic_greeting_reply(text: str | None) -> bool:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return False
    folded = _fold(cleaned)
    if any(folded == _fold(variant) for variant in greeting_variants()):
        return True
    if "eu sou o crono" in folded and len(cleaned) <= 220:
        return True
    return bool(_GREETING_BODY_RE.match(cleaned))


def recent_assistant_replies(
    recent_turns: list[dict[str, Any]] | None,
    *,
    limit: int = 8,
) -> list[str]:
    """Most recent assistant texts for this conversation (newest last)."""
    replies: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            replies.append(content)
    return replies[-limit:]


def last_assistant_content(recent_turns: list[dict[str, Any]] | None) -> str | None:
    replies = recent_assistant_replies(recent_turns, limit=1)
    return replies[-1] if replies else None


def already_said(
    candidate: str,
    recent_turns: list[dict[str, Any]] | None,
    *,
    lookback: int = 8,
) -> bool:
    """True if this exact reply (normalized) was already sent to this person."""
    folded = _fold(candidate)
    if not folded:
        return False
    for previous in recent_assistant_replies(recent_turns, limit=lookback):
        if _fold(previous) == folded:
            return True
    return False


def choose_greeting_reply(recent_turns: list[dict[str, Any]] | None = None) -> str:
    """Pick a greeting that was not already sent in this conversation.

    Prefer the active persona greeting (Crono) so every customer hears the
    same identity; only rotate canned fallbacks when that phrase was already
    used in this thread.
    """
    for variant in greeting_variants():
        if not already_said(variant, recent_turns):
            return variant

    # All greetings already used — still avoid repeating the last one.
    fallback = "Pode me dizer o que você precisa?"
    if not already_said(fallback, recent_turns):
        return fallback
    return "Estou aqui — o que você procura?"


def is_farewell_message(text: str | None) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    return bool(_FAREWELL_RE.match(_fold(cleaned)))


def choose_farewell_reply(name: str | None = None) -> str:
    first = str(name or "").strip().split()[0] if name and str(name).strip() else None
    if first:
        return f"Até, {first}! Qualquer coisa, é só chamar."
    return "Até! Qualquer coisa, é só chamar."
