"""Official NewStore institutional policies available to the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.persona.site_knowledge import TRADE_IN_HANDOFF_MESSAGE, STORE_URL


class StoreKnowledgeProvider(Protocol):
    """Internal seam for NewStore policy/institutional knowledge."""

    def lookup(self, question: str) -> str | None:
        ...


class NewStoreKnowledgeProvider:
    """Deterministic store policies (trade-in, evaluation, buy-back)."""

    def lookup(self, question: str) -> str | None:
        normalized = (question or "").lower()
        if not normalized:
            return None
        trade_cues = (
            "seminovo",
            "usado",
            "troca",
            "avalia",
            "avaliação",
            "avaliacao",
            "compram",
            "comprando",
            "trade",
        )
        if any(cue in normalized for cue in trade_cues):
            return (
                "A New Store avalia, troca e compra relógios. "
                "Esse fluxo é feito por atendente humano da loja — "
                "não negar a política e não inventar valores de avaliação."
            )
        return None


# Backward-compatible name (previously an empty stub).
EmptyStoreKnowledgeProvider = NewStoreKnowledgeProvider

DEFAULT_STORE_KNOWLEDGE = NewStoreKnowledgeProvider()


def lookup_store_policy(question: str) -> str | None:
    return DEFAULT_STORE_KNOWLEDGE.lookup(question)


def trade_in_policy_text() -> str:
    return TRADE_IN_HANDOFF_MESSAGE


@dataclass(frozen=True)
class EvidencePackage:
    """Minimal institutional knowledge bundle for prompt injection (no prices)."""

    items: list[dict[str, str]] = field(default_factory=list)

    def as_relevant_knowledge(self) -> list[dict[str, str]]:
        return list(self.items)


_INSTITUTIONAL_SNIPPETS: tuple[dict[str, Any], ...] = (
    {
        "title": "Troca e avaliação",
        "cues": (
            "seminovo",
            "usado",
            "troca",
            "avalia",
            "avaliação",
            "avaliacao",
            "compram",
            "comprando",
            "trade",
        ),
        "body": (
            "A New Store avalia, troca e compra relógios. "
            "Esse fluxo é feito por atendente humano da loja — "
            "não negar a política e não inventar valores de avaliação."
        ),
    },
    {
        "title": "Garantia e autenticidade",
        "cues": (
            "garantia",
            "autentic",
            "original",
            "certificado",
            "procedência",
            "procedencia",
        ),
        "body": (
            "Relógios vendidos pela New Store seguem política de garantia e "
            "procedência do fabricante/distribuidor. "
            "Não invente prazo ou cobertura — confirme com tools ou encaminhe ao humano."
        ),
    },
    {
        "title": "Entrega e pronta-entrega",
        "cues": (
            "frete",
            "entrega",
            "envio",
            "pronta entrega",
            "pronta-entrega",
            "cep",
        ),
        "body": (
            "Prazos e valores de frete vêm somente de cotação oficial (Tray/tools). "
            "Peças em pronta-entrega podem ter prazo reduzido — confirme no catálogo, "
            "sem inventar dias ou valores."
        ),
    },
    {
        "title": "Loja e atendimento",
        "cues": (
            "loja física",
            "loja fisica",
            "endereço",
            "endereco",
            "horário",
            "horario",
            "atendimento humano",
        ),
        "body": (
            f"A New Store Relógios opera pelo site {STORE_URL} e canais digitais. "
            "Para avaliação de seminovos ou casos complexos, encaminhe ao atendente humano."
        ),
    },
)


def _persona_institutional_items(metadata: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("institutionalKnowledge") or metadata.get(
        "institutional_knowledge"
    )
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body") or entry.get("content") or entry.get("text") or "").strip()
        if not body:
            continue
        title = str(entry.get("title") or entry.get("slug") or "institucional").strip()
        items.append({"title": title, "body": body})
    return items


def fetch_institutional_knowledge(
    message_text: str | None,
    *,
    persona_metadata: dict[str, Any] | None = None,
) -> EvidencePackage:
    """Return curated institutional snippets matched to the user message (no prices)."""
    normalized = (message_text or "").casefold()
    items: list[dict[str, str]] = []
    seen_titles: set[str] = set()

    for entry in _INSTITUTIONAL_SNIPPETS:
        cues = entry.get("cues") or ()
        if normalized and not any(str(cue).casefold() in normalized for cue in cues):
            continue
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not body or title.casefold() in seen_titles:
            continue
        seen_titles.add(title.casefold())
        items.append({"title": title, "body": body})

    for entry in _persona_institutional_items(persona_metadata):
        title_key = entry["title"].casefold()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        items.append(entry)

    return EvidencePackage(items=items)
