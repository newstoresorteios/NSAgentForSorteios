from __future__ import annotations

import json
import re
from typing import Any

from openai import APIError
import app.catalog.retrieval.runtime as _runtime
from app.models import SalesInterpretation
from app.ops.turn_runtime import LLMCallBudgetExceeded
from app.catalog.retrieval.availability import product_availability_state
from app.catalog.retrieval.limits import candidate_pool_limit, rerank_selection_limit
from app.catalog.retrieval.scoring import compact_candidates, semantic_preferences
from app.catalog.retrieval.text import _fold, _product_text
from app.catalog.retrieval.tokens import (
    expand_color_aliases,
    preference_color_tokens,
    preference_gender_tokens,
    product_matches_color_tokens,
    product_matches_gender_tokens,
)
from app.catalog.retrieval.types import ProductRerankSelection

def deterministic_semantic_order(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    from app.catalog.specs.catalog_specs import (
        extract_case_size_mm,
        extract_water_resistance_m,
        interpretation_wants_diver,
        interpretation_wants_small_case,
        is_false_diver_product,
        is_true_diver_product,
    )

    gender_tokens = preference_gender_tokens(interpretation)
    color_tokens = preference_color_tokens(interpretation)
    prefs = interpretation.preferences
    wants_diver = interpretation_wants_diver(interpretation)
    wants_small_case = interpretation_wants_small_case(interpretation)

    terms = [
        _fold(value)
        for value in (
            prefs.style,
            prefs.color,
            prefs.material,
            prefs.occasion,
            prefs.recipient,
            *prefs.attributes,
        )
        if value
    ]
    scored = []
    for index, product in enumerate(products):
        text = _product_text(product)
        base = sum(1 for term in terms if term in text)
        # Strong boost when catalog text evidences requested gender.
        if gender_tokens and product_matches_gender_tokens(product, gender_tokens):
            base += 3
        if color_tokens and product_matches_color_tokens(product, color_tokens):
            base += 4
        if wants_diver:
            wr = extract_water_resistance_m(product)
            if is_true_diver_product(product) or (wr is not None and wr >= 200):
                base += 6
            if is_false_diver_product(product):
                base -= 8
            elif wr is not None and wr <= 100:
                base -= 6
        if wants_small_case:
            size_raw = extract_case_size_mm(product)
            try:
                size = int(size_raw) if size_raw else None
            except (TypeError, ValueError):
                size = None
            if size is not None:
                if 35 <= size <= 40:
                    base += 4
                elif size >= 41:
                    base -= 2
            else:
                size_match = re.search(r"\b(3[5-9]|40)\s*mm\b", text)
                if size_match:
                    base += 3
                elif re.search(r"\b(4[1-9]|5\d)\s*mm\b", text):
                    base -= 2
        scored.append((base, index, product))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [product for _, _, product in scored[:rerank_selection_limit()]]


async def rerank_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    available_products = [
        product for product in products
        if product_availability_state(product) != "unavailable"
    ]
    settings = _runtime.get_settings()
    selection_limit = rerank_selection_limit()
    pool_limit = candidate_pool_limit()
    fallback = deterministic_semantic_order(available_products, interpretation)
    if not available_products or not settings.openai_api_key:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "selection_limit": selection_limit,
        })
        return fallback

    # Cap what the LLM may see — never the whole catalog.
    pool = available_products[:pool_limit]
    from app.catalog.index.catalog_index import (
        build_allowed_id_sets,
        reject_unknown_rerank_ids,
    )

    candidate_by_id = {
        str(product["id"]): product
        for product in pool
        if product.get("id") is not None
    }
    allowed_sets = build_allowed_id_sets(pool)
    allowed_ids = allowed_sets["allowed_product_ids"]
    prior_order = [str(p["id"]) for p in pool if p.get("id") is not None]
    try:
        from app.llm.openai_errors import OpenAIGatewayError
        from app.llm.openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=ProductRerankSelection,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classifique produtos reais da NewStore conforme as preferências. "
                        f"Retorne no máximo {selection_limit} IDs presentes em CANDIDATES, "
                        "em ordem de relevância. "
                        "Trate sinônimos de cor (azul=blue, preto=black, branco=white, rosa=pink, "
                        "verde=green, vermelho=red) e gênero (feminino/lady/dama). "
                        "Se o cliente pediu diver/mergulho, priorize 200m/diver/Aquascaphe/DS Action "
                        "e não ranqueie alto modelos dress/100m (ex.: DS-7) como diver. "
                        "Se pediu caixa menor, prefira ~37–40 mm sobre 41 mm+. "
                        "Não invente IDs. Não altere preço, estoque, URL ou disponibilidade. "
                        "Use só evidências dos candidatos (nome, marca, cor, descrição)."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "PREFERENCES": semantic_preferences(interpretation),
                        "COLOR_ALIASES": {
                            token: sorted(expand_color_aliases(token))
                            for token in preference_color_tokens(interpretation)
                        },
                        "ALLOWED_PRODUCT_IDS": sorted(allowed_ids),
                        "ALLOWED_VARIANT_IDS": sorted(
                            allowed_sets["allowed_variant_ids"]
                        ),
                        "CANDIDATES": compact_candidates(pool, limit=pool_limit),
                    }, ensure_ascii=False),
                },
            ],
            call_type="product_selection",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, ProductRerankSelection):
            raise ValueError("reranker_schema_missing")
        ordered_ids, invalid_ids = reject_unknown_rerank_ids(
            list(parsed.selected_product_ids or []),
            allowed_ids,
            limit=selection_limit,
        )
        selected = [candidate_by_id[pid] for pid in ordered_ids]
        if not selected:
            selected = fallback
        print("[sales.reranker]", {
            "source": "openai",
            "candidate_count": len(pool),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "selection_limit": selection_limit,
            "prior_order_sample": prior_order[:5],
            "posterior_order_sample": ordered_ids[:5],
            "allowed_product_ids": len(allowed_ids),
            "allowed_variant_ids": len(allowed_sets["allowed_variant_ids"]),
        })
        return selected
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "error_type": type(exc).__name__,
            "selection_limit": selection_limit,
        })
        return fallback


_deterministic_semantic_order = deterministic_semantic_order
