from __future__ import annotations

import re
from typing import Any, Literal

from app.models import SalesInterpretation
from app.catalog.retrieval.availability import _known_unavailable
from app.catalog.retrieval.price import effective_price
from app.catalog.retrieval.runtime import log_swallowed
from app.catalog.retrieval.text import _fold, _product_text
from app.catalog.retrieval.tokens import (
    effective_product_reference,
    preference_color_tokens,
    product_compatible_with_requested_movement,
    product_matches_color_tokens,
    required_model_tokens,
)

def hard_filter_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"],
) -> list[dict[str, Any]]:
    """Apply mandatory filters. Prefer TurnUnderstanding hard constraints when present."""
    subject = interpretation.subject
    preferences = interpretation.preferences
    expected_brand = _fold(subject.brand)
    expected_model = _fold(subject.model)
    expected_reference = _fold(effective_product_reference(subject.reference))
    expected_ean = _fold(subject.ean)
    brand_exclusive = False
    exact_only = False
    hard_color = None
    hard_material = None
    try:
        from app.catalog.index.catalog_index import _hard_constraints_from_interpretation

        hard = _hard_constraints_from_interpretation(interpretation)
        expected_brand = _fold(hard.get("brand")) or expected_brand
        expected_reference = _fold(hard.get("reference")) or expected_reference
        expected_ean = _fold(hard.get("ean")) or expected_ean
        brand_exclusive = bool(hard.get("brand_exclusive"))
        exact_only = bool(hard.get("exact_only"))
        hard_color = _fold(hard.get("dial_color"))
        hard_material = _fold(hard.get("material"))
        if hard.get("budget_max") is not None:
            preferences = preferences.model_copy(
                update={"budget_max": hard.get("budget_max")}
            )
        if hard.get("budget_min") is not None:
            preferences = preferences.model_copy(
                update={"budget_min": hard.get("budget_min")}
            )
    except Exception as exc:
        log_swallowed("hard_filter.turn_constraints", exc)

    excluded_brands: list[str] = []
    try:
        from app.catalog.specs.catalog_specs import (
            excluded_brands_from_interpretation,
            product_matches_excluded_brand,
        )

        excluded_brands = excluded_brands_from_interpretation(interpretation)
        if excluded_brands and expected_brand:
            if any(_fold(brand) == expected_brand for brand in excluded_brands):
                expected_brand = ""
                brand_exclusive = False
    except Exception as exc:
        log_swallowed("hard_filter.excluded_brands", exc)
        excluded_brands = []

    selected: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict) or not product.get("id"):
            continue
        text = _product_text(product)
        if excluded_brands and product_matches_excluded_brand(product, excluded_brands):
            continue
        if expected_brand:
            candidate_brand = _fold(product.get("brand"))
            if candidate_brand and candidate_brand != expected_brand:
                continue
            if not candidate_brand and expected_brand not in text:
                continue
            if brand_exclusive and candidate_brand and candidate_brand != expected_brand:
                continue
        if expected_reference and _fold(product.get("reference")) != expected_reference:
            continue
        if expected_ean and _fold(product.get("ean")) != expected_ean:
            continue
        if not product_compatible_with_requested_movement(
            product,
            subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        color_tokens = preference_color_tokens(interpretation)
        if hard_color:
            color_tokens = tuple(dict.fromkeys((*color_tokens, hard_color)))
        # Exact identity searches still require color evidence (with aliases).
        # Recommendation keeps brand/category pool intact so the LLM/reranker
        # can match "azul" ↔ "blue" — unless the customer said "somente"/exact_only.
        require_color = mode == "exact" or (exact_only and bool(color_tokens or hard_color))
        if (
            require_color
            and color_tokens
            and not product_matches_color_tokens(product, color_tokens)
        ):
            continue
        if hard_material and exact_only:
            if hard_material not in text and hard_material not in _fold(product.get("material")):
                continue
        if mode == "exact" and expected_model:
            model_tokens = list(required_model_tokens(subject.model))
            if model_tokens and not all(token in text for token in model_tokens):
                continue
        price = effective_price(product)
        if preferences.budget_min is not None and (price is None or price < preferences.budget_min):
            continue
        if preferences.budget_max is not None and (price is None or price > preferences.budget_max):
            continue
        if mode == "recommendation" and _known_unavailable(product):
            continue
        selected.append(product)

    # Diver ask: drop dress/100m false divers when the pool already has true divers
    # (Certina DS-7 vs DS Action — contact 5548999490859, 25/08).
    try:
        from app.catalog.specs.catalog_specs import (
            interpretation_wants_diver,
            is_false_diver_product,
            is_true_diver_product,
        )

        if interpretation_wants_diver(interpretation) and selected:
            true_hits = [p for p in selected if is_true_diver_product(p)]
            if true_hits:
                filtered = [p for p in selected if not is_false_diver_product(p)]
                if filtered:
                    print(
                        "[sales.hard_filter.diver]",
                        {
                            "before": len(selected),
                            "after": len(filtered),
                            "true_divers": len(true_hits),
                            "dropped_false": len(selected) - len(filtered),
                        },
                    )
                    return filtered
    except Exception as exc:
        log_swallowed("hard_filter.diver", exc)

    # Case-size ask: drop watches outside the requested mm window when we have
    # structured sizes (Ricardo 36–38 mm loop — contact 5511937118008, 27/08).
    try:
        from app.catalog.specs.catalog_specs import (
            extract_case_size_mm,
            interpretation_case_size_range,
            product_matches_case_size_range,
        )

        case_range = interpretation_case_size_range(interpretation)
        if case_range and selected:
            min_mm, max_mm = case_range
            in_range = [
                product
                for product in selected
                if product_matches_case_size_range(product, min_mm, max_mm)
            ]
            if in_range:
                print(
                    "[sales.hard_filter.case_size]",
                    {
                        "before": len(selected),
                        "after": len(in_range),
                        "min_mm": min_mm,
                        "max_mm": max_mm,
                    },
                )
                return in_range
            sized = [
                product
                for product in selected
                if product.get("case_size") or extract_case_size_mm(product)
            ]
            if sized:
                print(
                    "[sales.hard_filter.case_size]",
                    {
                        "before": len(selected),
                        "after": 0,
                        "min_mm": min_mm,
                        "max_mm": max_mm,
                        "reason": "no_in_range_matches",
                    },
                )
                return []
    except Exception as exc:
        log_swallowed("hard_filter.case_size", exc)

    # Chronograph ask: keep chrono-capable rows when the pool already has them
    # (João contact 5548999490859 — diver Certina served for "crono").
    try:
        from app.catalog.specs.catalog_specs import message_wants_chronograph

        wants_chrono = message_wants_chronograph(
            " ".join(
                str(item)
                for item in (
                    getattr(interpretation.preferences, "style", None),
                    *list(interpretation.preferences.attributes or []),
                )
                if item
            )
        )
        if wants_chrono and selected:
            chrono_hits = [
                product
                for product in selected
                if re.search(
                    r"\b(cron[oó]grafo|chronograph|chrono)\b",
                    _product_text(product),
                    re.IGNORECASE,
                )
            ]
            if chrono_hits:
                print(
                    "[sales.hard_filter.chronograph]",
                    {"before": len(selected), "after": len(chrono_hits)},
                )
                return chrono_hits
    except Exception as exc:
        log_swallowed("hard_filter.chronograph", exc)
    return selected
