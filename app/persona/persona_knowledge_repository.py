"""ChatBo persona attachments + structured profile for prompt injection."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.persona.persona_models import PersonaVersion

ATTACHMENT_READY_STATUSES = ("processed", "ready")

_EMPTY_BLOCK = "<persona_knowledge>\n</persona_knowledge>"
_ATTACHMENT_HEADING_RE = re.compile(
    r"(?im)^\s*#{2,4}\s+[^\n]+\.(?:txt|md|pdf|docx?|rtf|csv)\s*$"
)

_RETRIEVAL_STOPWORDS = frozenset(
    {
        "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do",
        "dos", "e", "em", "essa", "esse", "esta", "este", "eu", "me",
        "na", "nas", "no", "nos", "o", "os", "ou", "para", "por", "que",
        "qual", "quero", "se", "tem", "uma", "um", "voce", "voces", "produto",
        "produtos", "relogio", "relogios", "loja", "cliente",
    }
)


def retrieval_tokens(text: str | None) -> set[str]:
    folded = unicodedata.normalize("NFKD", str(text or "").casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", folded)
        if token not in _RETRIEVAL_STOPWORDS
    }


def strip_embedded_attachment_appendix(instructions: str | None) -> str:
    """Remove attachment bodies appended to the persisted persona contract.

    ChatBo stores these bodies separately in ``agent_persona_attachments``.
    Keeping the same document in the persona text defeats turn-scoped
    retrieval, so the compiler retains the authored contract before the first
    file heading and retrieves the attachment independently.
    """
    text = str(instructions or "").strip()
    match = _ATTACHMENT_HEADING_RE.search(text)
    if match is None:
        return text
    core = text[: match.start()].rstrip()
    appendix = text[match.start() :].strip()
    if len(core) < 200 or len(appendix) < 100:
        return text
    return core


def _attachment_chunks(text: str, *, chunk_chars: int = 1400) -> list[str]:
    """Create bounded, paragraph-aware chunks without adding a dependency."""
    paragraphs = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [text.strip()]:
        pieces = (
            [paragraph]
            if len(paragraph) <= chunk_chars
            else [paragraph[i : i + chunk_chars] for i in range(0, len(paragraph), chunk_chars)]
        )
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > chunk_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def retrieve_attachment_sections(
    attachments: list[PersonaKnowledgeAttachment],
    query_text: str | None,
    *,
    max_chunks: int = 3,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Select source-labelled attachment chunks with evidence for this turn."""
    query_tokens = retrieval_tokens(query_text)
    if not query_tokens:
        return [], []
    ranked: list[tuple[int, int, str, str, str]] = []
    for attachment in attachments:
        filename_tokens = retrieval_tokens(attachment.filename)
        for index, chunk in enumerate(_attachment_chunks(attachment.extracted_text), start=1):
            chunk_tokens = retrieval_tokens(chunk)
            overlap = query_tokens & (chunk_tokens | filename_tokens)
            if not overlap:
                continue
            score = len(overlap) * 10 + len(query_tokens & filename_tokens) * 3
            ranked.append((score, -index, attachment.id, attachment.filename, chunk))
    ranked.sort(reverse=True)
    if not ranked:
        return [], []
    relative_floor = max(10, math.ceil(ranked[0][0] * 0.8))
    selected = [item for item in ranked if item[0] >= relative_floor][
        : max(1, max_chunks)
    ]
    ids = list(dict.fromkeys(item[2] for item in selected))
    sections = [
        (f"{filename} [source:{attachment_id}#chunk-{abs(negative_index)}]", chunk)
        for _, negative_index, attachment_id, filename, chunk in selected
    ]
    return ids, sections

# Full ChatBo agent_personas surface used by the attendance UI.
CHATBO_PROFILE_COLUMNS = (
    "name",
    "role",
    "segment",
    "language",
    "tone",
    "tone_details",
    "greeting",
    "introduction",
    "customer_address_style",
    "closing_message",
    "target_audience",
    "customer_profile",
    "sales_goals",
    "qualification_rules",
    "opportunity_criteria",
    "human_handoff_criteria",
    "objection_handling",
    "upsell_rules",
    "recommendation_rules",
    "escalation_rules",
    "restrictions",
    "examples",
    "status",
)

_TONE_LABELS = {
    "consultative": "Consultivo",
    "professional": "Profissional",
    "objective": "Objetivo",
    "friendly": "Amigável",
    "sophisticated": "Sofisticado",
    "technical": "Técnico",
    "informal": "Informal",
    "custom": "Personalizado",
    "personalized": "Personalizado",
}


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
    from app.db import get_conn

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
    from app.db import get_conn

    columns = ", ".join(CHATBO_PROFILE_COLUMNS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {columns}
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


def _format_profile_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2).strip()
    return str(value).strip()


def _tone_label(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return _TONE_LABELS.get(text.casefold(), text)


def iter_structured_persona_sections(
    profile: dict[str, Any],
) -> list[tuple[str, str]]:
    """Ordered ChatBo sections for prompt injection."""
    identity_bits = [
        ("Nome", profile.get("name")),
        ("Função", profile.get("role")),
        ("Segmento", profile.get("segment")),
        ("Idioma", profile.get("language")),
        ("Tom de voz", _tone_label(profile.get("tone"))),
        ("Status", profile.get("status")),
    ]
    identity_lines = [
        f"- {title}: {body}"
        for title, value in identity_bits
        if (body := _format_profile_value(value))
    ]
    sections: list[tuple[str, str]] = []
    if identity_lines:
        sections.append(("Identidade", "\n".join(identity_lines)))

    mapping: list[tuple[str, Any]] = [
        ("Detalhes do tom", profile.get("tone_details")),
        ("Saudação inicial", profile.get("greeting")),
        ("Apresentação do agente", profile.get("introduction")),
        ("Forma de chamar o cliente", profile.get("customer_address_style")),
        ("Encerramento padrão", profile.get("closing_message")),
        ("Público-alvo", profile.get("target_audience")),
        ("Tipo / perfil do cliente", profile.get("customer_profile")),
        ("Objetivos e prioridades de venda", profile.get("sales_goals")),
        ("Qualificação", profile.get("qualification_rules")),
        ("Critérios de oportunidade", profile.get("opportunity_criteria")),
        ("Critérios para encaminhar ao vendedor", profile.get("human_handoff_criteria")),
        ("Tratamento de objeções", profile.get("objection_handling")),
        ("Upsell", profile.get("upsell_rules")),
        ("Regras de recomendação", profile.get("recommendation_rules")),
        ("Escalação", profile.get("escalation_rules")),
        ("Restrições", profile.get("restrictions")),
        ("Exemplos", profile.get("examples")),
    ]
    for title, value in mapping:
        body = _format_profile_value(value)
        if body:
            sections.append((title, body))
    return sections


def format_structured_persona_profile(
    profile: dict[str, Any],
    *,
    instructions: str | None = None,
    skip_embedded_sections: bool = True,
) -> str:
    """Render ChatBo profile. Optionally skip sections already in instructions."""
    # Always keep short identity/attendance fields — they drive greeting and tone.
    always_include = {
        "Identidade",
        "Detalhes do tom",
        "Saudação inicial",
        "Apresentação do agente",
        "Forma de chamar o cliente",
        "Encerramento padrão",
    }
    sections: list[str] = []
    for title, body in iter_structured_persona_sections(profile):
        if (
            skip_embedded_sections
            and title not in always_include
            and instructions
            and text_already_embedded(body, instructions)
        ):
            continue
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
        else:
            chunks.append(piece[: max(0, budget - 20)] + "\n...[truncado]")
            budget = 0

    for filename, body in attachment_sections:
        if budget < 200:
            break
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
        "Base completa da persona ChatBo (identidade, tom, saudação, objetivos, "
        "qualificação, objeções, recomendação, handoff e restrições).\n"
        "Siga estas orientações no atendimento. "
        "Nunca use isto para inventar preço, estoque, URL ou status de pedido — tools/Tray prevalecem.\n"
    )
    return intro + "\n\n".join(chunks) + "\n</persona_knowledge>"


def load_persona_knowledge_for_prompt(
    persona: PersonaVersion,
    *,
    limit: int = 10,
    max_chars: int = 12000,
    relevant_knowledge: list[Any] | None = None,
    query_text: str | None = None,
) -> tuple[list[str], str]:
    """Return attachment ids used and the XML block for the compiled prompt."""
    persona_id = chatbo_persona_id(persona.metadata)
    instructions = strip_embedded_attachment_appendix(persona.instructions)
    attachment_sections: list[tuple[str, str]] = []
    attachment_ids: list[str] = []

    if persona_id:
        available_attachments: list[PersonaKnowledgeAttachment] = []
        for attachment in list_persona_attachments(persona_id, limit=limit):
            if text_already_embedded(attachment.extracted_text, instructions):
                continue
            available_attachments.append(attachment)
        if query_text is None:
            attachment_sections = [
                (attachment.filename, attachment.extracted_text)
                for attachment in available_attachments
            ]
            attachment_ids = [attachment.id for attachment in available_attachments]
        else:
            attachment_ids, attachment_sections = retrieve_attachment_sections(
                available_attachments,
                query_text,
            )

        profile = get_chatbo_persona_profile(persona_id) or {}
        # Always prefer ChatBo structured fields; skip only sections already
        # present verbatim in instructions to avoid doubling tokens.
        profile_text = format_structured_persona_profile(
            profile,
            instructions=instructions,
            skip_embedded_sections=True,
        )
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
