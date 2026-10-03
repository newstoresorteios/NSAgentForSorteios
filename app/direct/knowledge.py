"""Published institutional knowledge and persona attachments, without legacy prompts."""
from __future__ import annotations

import json
import hashlib
import math
import re
import unicodedata
from collections import Counter


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


def document_id(doc):
    return hashlib.sha256((doc['source']+'\n'+doc['title']+'\n'+doc['text']).encode()).hexdigest()[:20]


def _tokens(text):
    from app.persona.persona_knowledge_repository import retrieval_tokens
    folded = ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c))
    return retrieval_tokens(folded)


_RELATED = (
    'garantia defeito quebrou conserto reparo assistencia',
    'devolucao devolver arrependimento desistir reembolso',
    'troca trocar tamanho substituicao',
    'entrega envio postagem frete demora prazo chegar',
    'original autenticidade falsificado replica procedencia',
    'pagamento parcelamento parcelas cartao pix desconto',
)


def search_knowledge(documents, query: str) -> dict:
    from app.persona.persona_knowledge_repository import _attachment_chunks
    words = _tokens(query)
    expanded = set(words)
    for group in _RELATED:
        tokens = set(group.split())
        if words & tokens:
            expanded |= tokens
    chunks = []
    seen = set()
    for doc in documents:
        identity = document_id(doc)
        for index, chunk in enumerate(_attachment_chunks(doc['text'], chunk_chars=1600)):
            digest = hashlib.sha256(chunk.encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            chunks.append((doc, identity, index, chunk, _tokens(chunk), _tokens(doc['title'])))
    frequency = Counter(token for *_, tokens, title in chunks for token in tokens | title)
    ranked = []
    for doc, identity, index, chunk, tokens, title in chunks:
        score = sum((2 if token in words else .4) * math.log(1 + len(chunks)/(1+frequency[token]))
                    * (2 if token in title else 1) for token in expanded & (tokens | title))
        if score:
            ranked.append((score, {'document_id': identity, 'chunk': index, 'title': doc['title'],
                                   'text': chunk, 'source': doc['source']}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    # Diversify before filling remaining slots so a long policy cannot hide others.
    selected, ids = [], set()
    for _, item in ranked:
        if item['document_id'] not in ids and len(selected) < 4:
            selected.append(item); ids.add(item['document_id'])
    for _, item in ranked:
        if item not in selected and len(selected) < 6:
            selected.append(item)
    return {'documents': selected, 'source': 'published_store_knowledge',
            'matched_chunks': len(ranked), 'has_more': len(ranked)>len(selected),
            'instruction': 'Use read_knowledge_document para ler outras partes. Ausência de trecho não comprova ausência de política.'}


def read_document(documents, identity, offset=0):
    doc = next((d for d in documents if document_id(d) == identity), None)
    if not doc:
        return {'ok': False, 'error': 'document_version_not_found'}
    end = offset + 8000
    return {'ok': True, 'document_id': identity, 'title': doc['title'], 'source': doc['source'],
            'text': doc['text'][offset:end], 'offset': offset,
            'next_offset': end if end < len(doc['text']) else None}
