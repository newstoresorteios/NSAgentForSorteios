from __future__ import annotations

from typing import Any

from app.models import SalesInterpretation
from app.catalog.retrieval.availability import _truth_state
from app.catalog.retrieval.limits import MAX_VARIANT_PRODUCT_QUERIES, ToolExecutor
from app.catalog.retrieval.text import _fold

def _needs_variant_evidence(interpretation: SalesInterpretation) -> bool:
    preferences = interpretation.preferences
    return bool(
        preferences.color
        or preferences.material
        or preferences.attributes
        or "inventory" in interpretation.information_needed
    )


async def enrich_product_variants(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    execute_tool: ToolExecutor,
) -> list[dict[str, Any]]:
    needs_evidence = _needs_variant_evidence(interpretation)
    from app.catalog.retrieval.rerank import deterministic_semantic_order
    candidates = deterministic_semantic_order(products, interpretation)
    candidate_ids = {
        str(product["id"])
        for product in candidates[:MAX_VARIANT_PRODUCT_QUERIES]
        if product.get("id") is not None
    }
    enriched: list[dict[str, Any]] = []
    products_checked = 0
    variants_loaded = 0
    matched_preferences = 0
    preference_terms = [
        _fold(value)
        for value in (
            interpretation.preferences.color,
            interpretation.preferences.material,
            *interpretation.preferences.attributes,
        )
        if value
    ]
    for product in products:
        product_id = str(product.get("id")) if product.get("id") is not None else ""
        should_check = product_id in candidate_ids and (
            needs_evidence or _truth_state(product.get("has_variation")) is True
        )
        if not should_check:
            enriched.append(product)
            continue
        products_checked += 1
        result = await execute_tool(
            "list_product_variants",
            {"product_id": product_id},
        )
        if "error" in result:
            enriched.append(product)
            continue
        variants = result.get("variants") if isinstance(result.get("variants"), list) else []
        variants_loaded += len(variants)
        matched_preferences += sum(
            1
            for variant in variants
            if any(term in _fold(variant) for term in preference_terms)
        )
        enriched.append({**product, "variants": variants})
    print("[sales.variants]", {
        "products_checked": products_checked,
        "variants_loaded": variants_loaded,
        "matched_preferences": matched_preferences,
    })
    return enriched
