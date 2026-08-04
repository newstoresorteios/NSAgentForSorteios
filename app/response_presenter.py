"""Presentation rules for natural, channel-aware outbound replies (Phase 12)."""

from __future__ import annotations

import re
from typing import Any

from .channel_profiles import ChannelProfile, get_channel_profile
from .models import AgentResult, IncomingMessage


_GENERIC_OPENERS = re.compile(
    r"^\s*(claro[!.,\s]*|com certeza[!.,\s]*|sera um prazer[!.,\s]*|"
    r"será um prazer[!.,\s]*|com o maior prazer[!.,\s]*)",
    flags=re.IGNORECASE,
)
_REPEATED_NAME = re.compile(
    r"^\s*(olá|ola|oi)[,\s]+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁ-ú'-]{1,40})[!,.\s]+",
    flags=re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
_QUESTION_RE = re.compile(r"\?")
_CTA_RE = re.compile(
    r"\b(quer que eu|posso (?:te )?ajudar|deseja|vamos finalizar|"
    r"posso preparar|segue o link)\b",
    flags=re.IGNORECASE,
)
_ROBOTIC_CLOSING = re.compile(
    r"(?:\n|^)\s*(estou (?:à|a) disposi[cç][aã]o|"
    r"qualquer d[uú]vida[^.!\n]*|"
    r"fico no aguardo)[.!]?\s*$",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)


def strip_generic_opener(text: str) -> str:
    return _GENERIC_OPENERS.sub("", text or "", count=1).lstrip(" ,.-")


def limit_questions(text: str, *, max_questions: int = 1) -> str:
    if max_questions < 0:
        return text or ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", (text or "").strip())
        if part.strip()
    ]
    kept: list[str] = []
    questions = 0
    for sentence in sentences:
        is_question = "?" in sentence
        if is_question:
            if questions >= max_questions:
                continue
            questions += 1
        kept.append(sentence)
    return " ".join(kept).strip() if kept else (text or "").strip()


def soften_emoji_excess(text: str, *, max_emoji_runs: int = 1) -> str:
    runs = list(_EMOJI_RE.finditer(text or ""))
    if len(runs) <= max_emoji_runs:
        return text or ""
    out = text or ""
    # Remove from the end so earlier expressive emoji can remain.
    for match in reversed(runs[max_emoji_runs:]):
        out = out[: match.start()] + out[match.end() :]
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def strip_robotic_closing(text: str) -> str:
    return _ROBOTIC_CLOSING.sub("", text or "").rstrip()


def split_whatsapp_blocks(text: str, *, max_blocks: int = 3) -> str:
    chunks = [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]
    if len(chunks) <= max_blocks:
        return text or ""
    # Keep first blocks; fold remainder into the last allowed block.
    head = chunks[: max_blocks - 1]
    tail = " ".join(chunks[max_blocks - 1 :])
    return "\n\n".join([*head, tail]).strip()


def preserve_urls(original: str, rewritten: str) -> str:
    """Ensure presentation rewrites do not truncate URLs present originally."""
    original_urls = _URL_RE.findall(original or "")
    if not original_urls:
        return rewritten
    out = rewritten or ""
    for url in original_urls:
        cleaned = url.rstrip(".,;:!?)]}\"'" )
        if cleaned and cleaned not in out:
            out = f"{out.rstrip()}\n{cleaned}".strip()
    return out


def mark_similar_product_language(text: str, metadata: dict[str, Any]) -> str:
    similar = bool(
        metadata.get("similar_product")
        or metadata.get("match_kind") == "similar"
        or (metadata.get("retrieval") or {}).get("match_kind") == "similar"
    )
    if not similar:
        return text
    lowered = (text or "").casefold()
    if "semelhante" in lowered or "parecido" in lowered or "similar" in lowered:
        return text
    prefix = "Encontrei opções semelhantes (não é o modelo exato).\n"
    return prefix + (text or "").lstrip()


def present_reply_text(
    text: str,
    *,
    channel: str | None,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
    profile: ChannelProfile | None = None,
) -> str:
    profile = profile or get_channel_profile(channel)
    metadata = metadata or {}
    original = text or ""
    value = strip_generic_opener(original)
    value = strip_robotic_closing(value)
    value = soften_emoji_excess(value, max_emoji_runs=1 if profile.channel != "widget" else 2)
    value = mark_similar_product_language(value, metadata)

    max_questions = 1
    if profile.channel in {"instagram", "facebook"}:
        max_questions = 1
    elif intent in {"handoff", "out_of_scope"}:
        max_questions = 0
    value = limit_questions(value, max_questions=max_questions)

    # Avoid stacking multiple CTAs on WhatsApp/social.
    if profile.channel in {"whatsapp", "instagram", "facebook"}:
        cta_matches = list(_CTA_RE.finditer(value))
        if len(cta_matches) > 1:
            # Keep first CTA sentence; drop later CTA sentences loosely.
            sentences = re.split(r"(?<=[.!?])\s+", value)
            kept: list[str] = []
            seen_cta = False
            for sentence in sentences:
                has_cta = bool(_CTA_RE.search(sentence))
                if has_cta and seen_cta:
                    continue
                if has_cta:
                    seen_cta = True
                kept.append(sentence)
            value = " ".join(kept).strip()

    if profile.channel == "whatsapp":
        value = split_whatsapp_blocks(value, max_blocks=3)
    elif profile.channel in {"instagram", "facebook"}:
        # Prefer compact single/double block replies.
        value = split_whatsapp_blocks(value, max_blocks=2)

    # Soft-greeting / thanks: never force a sales CTA rewrite — only clean.
    if intent in {"general", "greeting"} and not metadata.get("used_tray"):
        value = limit_questions(value, max_questions=1)

    return preserve_urls(original, value).strip()


def present_agent_result(
    incoming: IncomingMessage,
    result: AgentResult,
) -> AgentResult:
    profile = get_channel_profile(incoming.channel)
    metadata = dict(result.response_metadata or {})
    presented = present_reply_text(
        result.reply_text or "",
        channel=incoming.channel,
        intent=result.intent,
        metadata=metadata,
        profile=profile,
    )
    result.reply_text = presented
    result.response_metadata["presentation"] = {
        "channel": profile.channel,
        "tone": profile.tone,
        "max_blocks": 3 if profile.channel == "whatsapp" else 2,
        "max_questions": 0 if result.intent in {"handoff", "out_of_scope"} else 1,
        "rules": [
            "answer_first",
            "no_generic_opener",
            "one_main_question",
            "preserve_urls",
            "controlled_cta",
        ],
    }
    return result
