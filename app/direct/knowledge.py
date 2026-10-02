"""Published institutional knowledge and persona attachments, without legacy prompts."""
from __future__ import annotations

import json


def knowledge_documents(persona) -> list[dict[str, str]]:
    from app.persona.store_knowledge import _INSTITUTIONAL_SNIPPETS
    from app.persona.persona_knowledge_repository import list_persona_attachments
    documents = [{"title": str(item.get("title") or "Política da loja"),
                  "text": str(item.get("body") or ""),
                  "source": str(item.get("sourceUrl") or "")}
                 for item in _INSTITUTIONAL_SNIPPETS() if item.get("body")]
    active = getattr(persona, "active_persona", None)
    metadata = getattr(active, "metadata", None) or {}
    entries = metadata.get("institutionalKnowledge") or metadata.get("institutional_knowledge") or []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            body = str(entry.get("body") or entry.get("content") or entry.get("text") or "").strip()
            if body:
                documents.append({"title": str(entry.get("title") or entry.get("slug") or "Institucional"),
                                  "text": body,
                                  "source": str(entry.get("sourceUrl") or entry.get("source_url") or "persona")})
    if persona.chatbo_persona_id:
        documents.extend({"title": item.filename, "text": item.extracted_text, "source": item.id}
                         for item in list_persona_attachments(persona.chatbo_persona_id))
    return documents


def vector_store_for(settings, workspace_id: str) -> str | None:
    mapping = json.loads(settings.direct_vector_stores)
    if not isinstance(mapping, dict):
        raise ValueError("direct_vector_stores_invalid")
    value = mapping.get(workspace_id)
    if value is not None and (not isinstance(value, str) or not value.startswith("vs_")):
        raise ValueError("direct_vector_store_invalid")
    return value


def search_knowledge(documents, query: str) -> dict:
    from app.persona.persona_knowledge_repository import retrieval_tokens, _attachment_chunks
    words = retrieval_tokens(query)
    ranked = []
    for doc in documents:
        for chunk in _attachment_chunks(doc["text"], chunk_chars=1600):
            score = len(words & retrieval_tokens(doc["title"] + " " + chunk))
            if score:
                ranked.append((score, {"title": doc["title"], "text": chunk, "source": doc["source"]}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return {"documents": [item for _, item in ranked[:6]], "source": "published_store_knowledge"}
