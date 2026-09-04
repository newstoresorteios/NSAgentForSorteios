"""Single catalog ranking authority.

Live order:
- recommendation → ``hybrid_rank_products``
- exact / leftover plan dict → ``score_catalog_candidates``

``sales.workflows.catalog_ranking.rank_candidates`` is leftover. Shadow
compares it (or the other scorer) and never rewrites the live list.
"""

from __future__ import annotations

from typing import Any, Literal

from app.catalog.retrieval.limits import customer_result_limit
from app.catalog.retrieval.scoring import score_catalog_candidates
from app.models import SalesInterpretation


def product_rank_ids(products: list[dict[str, Any]] | None) -> list[str]:
    ids: list[str] = []
    for product in products or []:
        if not isinstance(product, dict):
            continue
        token = product.get("id") or product.get("product_id")
        if token is None:
            continue
        text = str(token).strip()
        if text:
            ids.append(text)
    return ids


def rank_catalog_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"] = "exact",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Deterministic rank used when the compiled presenter is not in play."""
    cap = customer_result_limit() if limit is None else max(1, int(limit))
    if mode == "recommendation":
        from app.catalog.index.catalog_index import hybrid_rank_products

        return hybrid_rank_products(
            products,
            interpretation,
            mode="recommendation",
        )[:cap]
    return score_catalog_candidates(products, interpretation, limit=cap)


def shadow_compare_rank(
    *,
    live_ids: list[str],
    other_ids: list[str],
    other_name: str,
    mode: str,
) -> bool:
    """Log when a leftover ranker would show a different top list. Never mutates."""
    live = list(live_ids)
    other = list(other_ids)
    agree = live == other
    print(
        "[catalog.rank.shadow]",
        {
            "mode": mode,
            "other": other_name,
            "agree": agree,
            "live_ids": live,
            "other_ids": other,
        },
    )
    return agree
