"""Deterministic AI identification, applied to the final customer-visible reply.

No model call, no business facts and no default workspace identity. The stored
header is reused on delivery/retry, after the turn's persona context is gone.
"""
from __future__ import annotations

import re
from typing import Any

from app.models import AgentResult, IncomingMessage


_HEADER = re.compile(r"^[^\n]{0,120}Assistente virtual \(IA\)[^\n]*\n", re.I)
_COMPANY = re.compile(r"assistente virtual\s+(?:da|do|de)\s+([^.!?\n]{1,80})", re.I)


def _identity() -> tuple[str, str, str | None]:
    from app.persona.persona_runtime import get_persona_runtime

    persona = get_persona_runtime()
    if persona is None or not persona.loaded or not persona.enabled or persona.load_error:
        return "Assistente virtual (IA)", "Olá! Sou o assistente virtual (IA).", None
    name = " ".join(str(persona.agent_display_name or "").split())[:64]
    if not name:
        return "Assistente virtual (IA)", "Olá! Sou o assistente virtual (IA).", persona.workspace_id
    # Only the active workspace's published greeting can supply the company.
    match = _COMPANY.search(persona.greeting_text or "")
    company = f" da {match.group(1).strip()}" if match else ""
    return (
        f"{name} · Assistente virtual (IA){company}",
        f"Olá! Sou {name}, assistente virtual (IA){company}.",
        persona.workspace_id,
    )


def _needs_introduction(turns: list[dict[str, Any]] | None, incoming: IncomingMessage) -> bool:
    from app.memory.history_window import turns_for_conversation

    history = turns_for_conversation(turns, incoming.conversation_id)
    for turn in reversed(history):
        metadata = turn.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        disclosure = metadata.get("agent_disclosure")
        disclosure = disclosure if isinstance(disclosure, dict) else {}
        if turn.get("role") in {"human", "operator", "agent"} or metadata.get("actor_type") == "human":
            return True
        if turn.get("role") == "assistant":
            # A legacy response without disclosure must not count as an introduction.
            return not (
                disclosure.get("applied")
                or re.search(r"assistente virtual|intelig[eê]ncia artificial", str(turn.get("content") or ""), re.I)
            )
    return True


def apply_agent_disclosure(
    result: AgentResult,
    *,
    incoming: IncomingMessage | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    introduce: bool | None = None,
) -> AgentResult:
    """Idempotent; never let a late fallback lose the sender's AI identity."""
    metadata = result.response_metadata
    if metadata.get("actor_type") == "human" or not (result.reply_text or "").strip():
        return result
    prior = metadata.get("agent_disclosure")
    prior = prior if isinstance(prior, dict) else {}
    header, introduction, workspace = _identity()
    if prior.get("applied") and prior.get("header"):
        header = str(prior["header"])
        introduction = str(prior.get("introduction") or introduction)
        workspace = prior.get("workspace_id")
    text = (result.reply_text or "").strip()
    # Comparing the exact complete header avoids matching words inside a product.
    if text == header or text.startswith(header + "\n") or text == introduction or text.startswith(introduction + "\n"):
        return result
    text = _HEADER.sub("", text, count=1).lstrip()
    first = bool(introduce) if introduce is not None else bool(
        incoming is not None and _needs_introduction(recent_turns, incoming)
    )
    already_introduced = bool(re.search(r"\b(?:sou|somos)\b[^\n]{0,120}assistente virtual", text, re.I))
    prefix = introduction if first and not already_introduced else header
    result.reply_text = f"{prefix}\n\n{text}"
    metadata["agent_disclosure"] = {
        "version": 1, "applied": True, "actor_type": "ai", "header": header,
        "introduction": introduction, "introduced": first or already_introduced,
        "workspace_id": workspace,
    }
    metadata["actor_type"] = "ai"
    return result
