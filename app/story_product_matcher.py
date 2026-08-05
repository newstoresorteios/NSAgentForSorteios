"""Match Story visual evidence to real catalog candidates (tenant-scoped)."""

from __future__ import annotations

from typing import Any

from .catalog_index import build_allowed_id_sets, reject_unknown_rerank_ids
from .config import get_settings
from .fact_authority import catalog_item_key_for
from .instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding
from .observability import log_event


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def classify_match(
    candidates: list[StoryProductCandidate],
    *,
    multiple_products: bool,
) -> tuple[str, StoryProductCandidate | None]:
    settings = get_settings()
    auto_min = float(
        getattr(settings, "instagram_story_auto_match_min_confidence", 0.92) or 0.92
    )
    amb_min = float(
        getattr(settings, "instagram_story_ambiguous_min_confidence", 0.65) or 0.65
    )
    margin = float(getattr(settings, "instagram_story_match_margin", 0.12) or 0.12)
    if not candidates:
        return "not_found", None
    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))
    top1 = ordered[0]
    top2 = ordered[1] if len(ordered) > 1 else None
    gap = top1.score - (top2.score if top2 else 0.0)
    if (
        not multiple_products
        and top1.score >= auto_min
        and gap >= margin
        and top1.product_id
    ):
        return "matched", top1
    if top1.score >= amb_min:
        return "ambiguous", top1
    return "not_found", None


async def match_story_to_catalog(
    *,
    tenant_id: str,
    analysis: StoryVisualUnderstanding,
    execute_tool: Any | None = None,
) -> list[StoryProductCandidate]:
    """Build candidates from exact identifiers → index → visual traits.

    Never invents product IDs. execute_tool is optional Tray search helper.
    """
    _ = tenant_id  # enforced by repository/Tray callers upstream
    settings = get_settings()
    limit = int(getattr(settings, "instagram_story_max_candidates", 10) or 10)
    candidates: list[StoryProductCandidate] = []
    seen: set[str] = set()

    def _add(
        product: dict[str, Any],
        *,
        score: float,
        reasons: list[str],
        source: str,
    ) -> None:
        if not isinstance(product, dict) or product.get("id") is None:
            return
        pid = str(product["id"])
        vid = (
            str(product.get("variant_id"))
            if product.get("variant_id") is not None
            else None
        )
        key = catalog_item_key_for(pid, vid)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            StoryProductCandidate(
                catalog_item_key=key,
                product_id=pid,
                variant_id=vid,
                score=float(score),
                match_reasons=reasons,
                source=source,
            )
        )

    # Level 1 — exact identifiers via catalog index repository.
    try:
        from .catalog_index_repository import CatalogIndexRepository, row_to_product_dict

        repo = CatalogIndexRepository()
        for ean in analysis.visible_eans:
            for row in repo.search_exact(tenant_id=tenant_id, ean=str(ean)):
                _add(row_to_product_dict(row), score=0.99, reasons=[f"ean:{ean}"], source="exact_ean")
        for sku in analysis.visible_skus:
            for row in repo.search_exact(tenant_id=tenant_id, sku=str(sku)):
                _add(row_to_product_dict(row), score=0.98, reasons=[f"sku:{sku}"], source="exact_sku")
        for ref in analysis.visible_references:
            for row in repo.search_exact(tenant_id=tenant_id, reference=str(ref)):
                _add(
                    row_to_product_dict(row),
                    score=0.97,
                    reasons=[f"reference:{ref}"],
                    source="exact_reference",
                )
            for brand in analysis.visible_brands or analysis.logo_hypotheses:
                rows = repo.search_lexical(
                    tenant_id=tenant_id,
                    query=f"{brand} {ref}".strip(),
                    brand=brand,
                )
                for row in rows[:5]:
                    _add(
                        row_to_product_dict(row),
                        score=0.8,
                        reasons=[f"brand_ref:{brand}:{ref}"],
                        source="lexical",
                    )
    except Exception as exc:  # noqa: BLE001
        print("[story.matcher.index.error]", {"error_type": type(exc).__name__})

    # Level 3/4 — Tray complementary search when tool available and few candidates.
    if execute_tool is not None and len(candidates) < 3:
        query_bits = [
            *analysis.visible_brands[:1],
            *analysis.model_hypotheses[:1],
            *analysis.visible_references[:1],
            *analysis.dial_colors[:1],
        ]
        query = " ".join(str(x) for x in query_bits if x).strip()
        if query:
            try:
                result = await execute_tool("search_products", {"query": query, "limit": limit})
                products = result.get("products") if isinstance(result, dict) else None
                if isinstance(products, list):
                    for product in products[:limit]:
                        _add(
                            product,
                            score=0.7,
                            reasons=[f"tray_query:{query[:40]}"],
                            source="tray_search",
                        )
            except Exception as exc:  # noqa: BLE001
                print("[story.matcher.tray.error]", {"error_type": type(exc).__name__})

    # Soft visual traits boost among existing candidates only (no new IDs).
    dial = {_fold(c) for c in analysis.dial_colors}
    if dial and candidates:
        for candidate in candidates:
            # Without product payload we only keep prior score.
            if any(token in " ".join(candidate.match_reasons).casefold() for token in dial):
                candidate.score = min(1.0, candidate.score + 0.03)

    ordered = sorted(candidates, key=lambda c: (-c.score, c.product_id))[:limit]
    log_event(
        "instagram_story.candidates_found",
        {
            "count": len(ordered),
            "top_score": ordered[0].score if ordered else None,
            "multiple_products": analysis.multiple_products,
        },
    )
    return ordered


def reject_invented_rerank_ids(
    selected_ids: list[str],
    candidates: list[StoryProductCandidate],
) -> tuple[list[str], int]:
    allowed = {c.product_id for c in candidates}
    return reject_unknown_rerank_ids(
        selected_ids,
        allowed,
        limit=max(len(selected_ids), len(allowed), 1),
    )


def candidates_to_allowed_sets(
    candidates: list[StoryProductCandidate],
) -> dict[str, set[str]]:
    products = [
        {
            "id": c.product_id,
            "variant_id": c.variant_id,
            "catalog_item_key": c.catalog_item_key,
        }
        for c in candidates
    ]
    return build_allowed_id_sets(products)
