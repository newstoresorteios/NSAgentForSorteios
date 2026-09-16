"""Official-storefront fallback for exact text searches missed by Tray."""

from __future__ import annotations

import re
import unicodedata

from app.catalog.media.storefront_evidence import json_policy
from app.catalog.media.storefront_search import (
    search_storefront,
    storefront_products_from_hits,
)
from app.catalog.retrieval.session import RetrievalSession
from app.configuration.runtime import ConfigurationUnavailable, policy
from app.ops.observability import log_event


def _fold(value: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").lower())
        if not unicodedata.combining(char)
    )


def storefront_text_queries(session: RetrievalSession) -> list[str]:
    subject = session.interpretation.subject
    stopwords = {_fold(value) for value in json_policy("catalogStorefrontQueryStopwords")}
    raw_tokens = re.findall(r"[\w.-]+", _fold(session.message_text))
    raw_query = " ".join(token for token in raw_tokens if token not in stopwords)
    candidates = [
        str(subject.reference or "").strip(),
        raw_query,
        " ".join(
            part for part in (subject.brand, subject.model) if str(part or "").strip()
        ).strip(),
    ]
    return list(dict.fromkeys(query for query in candidates if len(query) >= 3))


def _matches_requested_type(product: dict, session: RetrievalSession) -> bool:
    product_type = _fold(session.interpretation.subject.product_type)
    if not product_type:
        return True
    stopwords = {_fold(value) for value in json_policy("catalogStorefrontQueryStopwords")}
    tokens = [
        token for token in re.findall(r"[\w-]+", product_type)
        if token not in stopwords or token in {"relogio", "pulseira"}
    ]
    if not tokens:
        return True
    identity = _fold(
        " ".join(
            str(product.get(key) or "")
            for key in ("name", "title", "url", "model")
        )
    )
    return any(token in identity for token in tokens)


async def run_storefront_fallback(session: RetrievalSession) -> int:
    """Add official storefront candidates when exact catalog retrieval misses."""
    if (
        session.retrieval_plan.mode != "exact"
        or session.hard_filtered
        or not str(session.message_text or "").strip()
        or session.product_lookup_failed
    ):
        return 0
    try:
        if not bool(policy("catalogStorefrontFallbackEnabled")):
            return 0
        max_queries = max(1, min(5, int(policy("catalogStorefrontFallbackMaxQueries"))))
        max_pages = max(1, min(10, int(policy("catalogStorefrontFallbackMaxPages"))))
        limit = max(1, min(120, int(policy("catalogStorefrontFallbackCandidateLimit"))))
        queries = storefront_text_queries(session)[:max_queries]
    except (ConfigurationUnavailable, TypeError, ValueError):
        return 0

    added = 0
    for query in queries:
        hits = await search_storefront(query, max_pages=max_pages, limit=limit)
        products = [
            product
            for product in storefront_products_from_hits(hits)
            if _matches_requested_type(product, session)
        ]
        before = len(session.candidates)
        session.absorb_products(products)
        session.refresh_hard_filtered()
        added += len(session.candidates) - before
        log_event(
            "catalog.storefront_text_fallback",
            {
                "query": query[:80],
                "hits": len(hits),
                "eligible": len(products),
                "added": len(session.candidates) - before,
                "matched": len(session.hard_filtered),
                "complete": bool(getattr(hits, "complete", True)),
            },
        )
        if session.hard_filtered:
            break
    return added
