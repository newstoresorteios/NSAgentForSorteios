"""Official NewStore institutional policies available to the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.persona.site_knowledge import TRADE_IN_HANDOFF_MESSAGE, STORE_URL


def trade_in_policy_text() -> str:
    from app.configuration.runtime import message, policy
    return message("trade_in_handoff" if policy("acceptsTradeIn") else "trade_in_unavailable")


@dataclass(frozen=True)
class EvidencePackage:
    """Minimal institutional knowledge bundle for prompt injection (no prices)."""

    items: list[dict[str, str]] = field(default_factory=list)

    def as_relevant_knowledge(self) -> list[dict[str, str]]:
        return list(self.items)


def _INSTITUTIONAL_SNIPPETS():
    import json
    from app.configuration.runtime import policy
    entries = json.loads(policy('business.institutional_knowledge'))
    for entry in entries:
        if entry.get('policyKey') == 'acceptsTradeIn':
            entry['body'] = trade_in_policy_text()
    return entries


def format_institutional_knowledge_block(
    message_text: str | None,
    *,
    persona_metadata: dict[str, Any] | None = None,
) -> str:
    """Cue-matched institutional snippets (no vector RAG, no new retrieval agent)."""
    from app.persona.persona_knowledge_repository import format_relevant_knowledge_block

    package = fetch_institutional_knowledge(
        message_text,
        persona_metadata=persona_metadata,
    )
    return format_relevant_knowledge_block(package.as_relevant_knowledge()).strip()


def _persona_institutional_items(
    metadata: dict[str, Any] | None,
    *,
    message_text: str | None = None,
) -> list[dict[str, str]]:
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("institutionalKnowledge") or metadata.get(
        "institutional_knowledge"
    )
    if not isinstance(raw, list):
        return []
    from app.persona.persona_knowledge_repository import retrieval_tokens

    query_tokens = retrieval_tokens(message_text)
    if not query_tokens:
        return []
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body") or entry.get("content") or entry.get("text") or "").strip()
        if not body:
            continue
        title = str(entry.get("title") or entry.get("slug") or "institucional").strip()
        if not (query_tokens & retrieval_tokens(f"{title} {body}")):
            continue
        item = {"title": title, "body": body}
        source_url = str(entry.get("sourceUrl") or entry.get("source_url") or "").strip()
        slug = str(entry.get("slug") or "").strip()
        reviewed_at = str(entry.get("reviewedAt") or entry.get("reviewed_at") or "").strip()
        if source_url:
            item["source_url"] = source_url
        if slug:
            item["slug"] = slug
        if reviewed_at:
            item["reviewed_at"] = reviewed_at
        items.append(item)
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

    for entry in _INSTITUTIONAL_SNIPPETS():
        cues = entry.get("cues") or ()
        if normalized and not any(str(cue).casefold() in normalized for cue in cues):
            continue
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not body or title.casefold() in seen_titles:
            continue
        seen_titles.add(title.casefold())
        item = {"title": title, "body": body}
        source_url = str(entry.get("sourceUrl") or entry.get("source_url") or "").strip()
        slug = str(entry.get("slug") or "").strip()
        reviewed_at = str(entry.get("reviewedAt") or entry.get("reviewed_at") or "").strip()
        if source_url:
            item["source_url"] = source_url
        if slug:
            item["slug"] = slug
        if reviewed_at:
            item["reviewed_at"] = reviewed_at
        items.append(item)

    for entry in _persona_institutional_items(
        persona_metadata,
        message_text=message_text,
    ):
        title_key = entry["title"].casefold()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        items.append(entry)

    return EvidencePackage(items=items)
