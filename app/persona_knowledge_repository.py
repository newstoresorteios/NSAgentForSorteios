"""ChatBo persona attachments + structured profile for prompt injection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .persona_models import PersonaVersion

ATTACHMENT_READY_STATUSES = ("processed", "ready")

_EMPTY_BLOCK = "<persona_knowledge>\n</persona_knowledge>"


@dataclass(frozen=True)
class PersonaKnowledgeAttachment:
    id: str
    filename: str
    extracted_text: str
    content_type: str | None = None


def chatbo_persona_id(metadata: dict[str, Any] | None) -> str | None:
    raw = (metadata or {}).get("chatboPersonaId")
    text = str(raw or "").strip()
    return text or None


def list_persona_attachments(
    chatbo_persona_id: str,
    *,
    limit: int = 10,
) -> list[PersonaKnowledgeAttachment]:
    from .db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, filename, content_type, extracted_text
                FROM public.agent_persona_attachments
                WHERE persona_id = %s::uuid
                  AND status = ANY(%s)
                  AND coalesce(trim(extracted_text), '') <> ''
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (chatbo_persona_id, list(ATTACHMENT_READY_STATUSES), limit),
            )
            rows = cur.fetchall() or []
    out: list[PersonaKnowledgeAttachment] = []
    for row in rows:
        text = str(row.get("extracted_text") or "").strip()
        if not text:
            continue
        out.append(
            PersonaKnowledgeAttachment(
                id=str(row.get("id") or ""),
                filename=str(row.get("filename") or "attachment"),
                extracted_text=text,
                content_type=str(row.get("content_type") or "") or None,
            )
        )
    return out


def get_chatbo_persona_profile(chatbo_persona_id: str) -> dict[str, Any] | None:
    from .db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    introduction,
                    target_audience,
                    customer_profile,
                    sales_goals,
                    qualification_rules,
                    opportunity_criteria,
                    human_handoff_criteria,
                    objection_handling,
                    upsell_rules,
                    recommendation_rules,
                    escalation_rules,
                    restrictions,
                    examples
                FROM public.agent_personas
                WHERE id = %s::uuid
                LIMIT 1
                """,
                (chatbo_persona_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def text_already_embedded(body: str, instructions: str, *, min_anchor: int = 80) -> bool:
    """Skip knowledge that was already merged into persona instructions."""
    cleaned = str(body or "").strip()
    if not cleaned:
        return True
    haystack = instructions.casefold()
    if len(cleaned) < min_anchor:
        return cleaned.casefold() in haystack
    head = cleaned[:min_anchor].casefold()
    tail = cleaned[-min(min_anchor, len(cleaned)) :].casefold()
    return head in haystack and tail in haystack


def format_structured_persona_profile(profile: dict[str, Any]) -> str:
    sections: list[str] = []
    mapping: list[tuple[str, Any]] = [
        ("Introdução complementar", profile.get("introduction")),
        ("Público-alvo", profile.get("target_audience")),
        ("Perfil do cliente", profile.get("customer_profile")),
        ("Objetivos de venda", profile.get("sales_goals")),
        ("Qualificação", profile.get("qualification_rules")),
        ("Critérios de oportunidade", profile.get("opportunity_criteria")),
        ("Handoff humano", profile.get("human_handoff_criteria")),
        ("Tratamento de objeções", profile.get("objection_handling")),
        ("Upsell", profile.get("upsell_rules")),
        ("Recomendações", profile.get("recommendation_rules")),
        ("Escalação", profile.get("escalation_rules")),
        ("Restrições", profile.get("restrictions")),
        ("Exemplos", profile.get("examples")),
    ]
    for title, value in mapping:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (dict, list)):
            body = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            body = str(value).strip()
        if body:
            sections.append(f"### {title}\n{body}")
    return "\n\n".join(sections)


def format_relevant_knowledge_block(items: list[Any]) -> str:
    if not items:
        return ""
    lines = ["<retrieved_knowledge>", "Contexto recuperado para esta conversa:"]
    for idx, item in enumerate(items, start=1):
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("slug") or f"doc_{idx}")
            body = str(item.get("body") or item.get("content") or item.get("text") or "")
        else:
            title = f"doc_{idx}"
            body = str(item)
        body = body.strip()
        if not body:
            continue
        lines.append(f"### {title}\n{body}")
    if len(lines) <= 2:
        return ""
    lines.append(
        "Use apenas para tom e políticas institucionais; preço/estoque/URL/pedido vêm das tools."
    )
    lines.append("</retrieved_knowledge>")
    return "\n".join(lines)


def format_persona_knowledge_block(
    *,
    attachment_sections: list[tuple[str, str]],
    profile_text: str = "",
    retrieved_text: str = "",
    max_chars: int = 12000,
) -> str:
    chunks: list[str] = []
    budget = max(500, max_chars)

    if profile_text.strip():
        header = "### Perfil estruturado (ChatBo)\n"
        piece = header + profile_text.strip()
        if len(piece) <= budget:
            chunks.append(piece)
            budget -= len(piece) + 2

    for filename, body in attachment_sections:
        piece = f"### Anexo: {filename}\n{body.strip()}"
        if len(piece) > budget:
            piece = piece[: max(0, budget - 20)] + "\n...[truncado]"
            chunks.append(piece)
            break
        chunks.append(piece)
        budget -= len(piece) + 2

    if retrieved_text.strip() and budget > 200:
        piece = retrieved_text.strip()
        if len(piece) > budget:
            piece = piece[: max(0, budget - 20)] + "\n...[truncado]"
        chunks.append(piece)

    if not chunks:
        return _EMPTY_BLOCK

    intro = (
        "<persona_knowledge>\n"
        "Base de conhecimento da persona (políticas e orientações institucionais).\n"
        "Nunca use isto para inventar preço, estoque, URL ou status de pedido — tools/Tray prevalecem.\n"
    )
    return intro + "\n\n".join(chunks) + "\n</persona_knowledge>"


def load_persona_knowledge_for_prompt(
    persona: PersonaVersion,
    *,
    limit: int = 10,
    max_chars: int = 12000,
    relevant_knowledge: list[Any] | None = None,
) -> tuple[list[str], str]:
    """Return attachment ids used and the XML block for the compiled prompt."""
    persona_id = chatbo_persona_id(persona.metadata)
    instructions = persona.instructions or ""
    attachment_sections: list[tuple[str, str]] = []
    attachment_ids: list[str] = []

    if persona_id:
        for attachment in list_persona_attachments(persona_id, limit=limit):
            if text_already_embedded(attachment.extracted_text, instructions):
                continue
            attachment_sections.append((attachment.filename, attachment.extracted_text))
            attachment_ids.append(attachment.id)

        profile = get_chatbo_persona_profile(persona_id) or {}
        profile_text = format_structured_persona_profile(profile)
        if profile_text and text_already_embedded(profile_text, instructions):
            profile_text = ""
    else:
        profile_text = ""

    retrieved_text = format_relevant_knowledge_block(list(relevant_knowledge or []))
    block = format_persona_knowledge_block(
        attachment_sections=attachment_sections,
        profile_text=profile_text if persona_id else "",
        retrieved_text=retrieved_text,
        max_chars=max_chars,
    )
    if block == _EMPTY_BLOCK and not retrieved_text:
        return attachment_ids, _EMPTY_BLOCK
    return attachment_ids, block
