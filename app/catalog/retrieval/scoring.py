from __future__ import annotations

import re
from typing import Any

from app.models import SalesInterpretation
from app.catalog.retrieval.limits import (
    CANDIDATE_POOL_LIMIT,
    CUSTOMER_RESULT_LIMIT,
    RERANK_SELECTION_LIMIT,
    SEMANTIC_MATCH_POOL_LIMIT,
)
from app.catalog.retrieval.text import _fold, _product_text
from app.catalog.retrieval.tokens import (
    _ACCESSORY_NAME_TOKENS,
    effective_product_reference,
    extract_model_codes,
    identity_core_tokens,
    is_prospex_diver_ask,
    model_excludes_gmt,
    preference_case_finish_tokens,
    preference_color_tokens,
    preference_feature_tokens,
    product_compatible_with_requested_movement,
    product_conflicts_dial_color,
    product_matches_case_finish_tokens,
    product_matches_color_tokens,
    product_matches_feature_tokens,
    required_model_tokens,
    _rejects_as_accessory,
)

def _compact_variants(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    compact: list[dict[str, Any]] = []
    for variant in value[:20]:
        if not isinstance(variant, dict):
            continue
        compact.append({
            key: item
            for key, item in {
                "variant_id": variant.get("variant_id") or variant.get("id"),
                "product_id": variant.get("product_id"),
                "name": variant.get("name"),
                "value": variant.get("value"),
                "color": variant.get("color"),
                "size": variant.get("size"),
                "version": variant.get("version"),
                "reference": variant.get("reference"),
                "sku": variant.get("sku") or variant.get("Sku"),
                "price": variant.get("price"),
                "promotional_price": variant.get("promotional_price"),
                "stock": variant.get("stock"),
                "available": variant.get("available"),
                "available_in_store": variant.get("available_in_store"),
                "availability": variant.get("availability"),
                "VariationSettings": variant.get("VariationSettings"),
            }.items()
            if item is not None
        })
    return compact or None


def score_catalog_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    require_color: bool = False,
    allow_movement_mismatch: bool = False,
    limit: int = RERANK_SELECTION_LIMIT,
) -> list[dict[str, Any]]:
    """Rank catalog rows by brand/model/color keyword overlap in the title."""
    color_tokens = preference_color_tokens(interpretation)
    feature_tokens = preference_feature_tokens(interpretation)
    case_tokens = preference_case_finish_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    model_codes = {
        _fold(code)
        for code in extract_model_codes(interpretation.subject.model)
    }
    brand_fold = _fold(interpretation.subject.brand)
    scored: list[tuple[int, dict[str, Any]]] = []
    for product in products:
        text = _product_text(product)
        candidate_brand = _fold(product.get("brand"))
        if brand_fold:
            if candidate_brand and candidate_brand != brand_fold:
                continue
            if not candidate_brand and brand_fold not in text:
                continue
        if identity_tokens and not all(token in text for token in identity_tokens):
            continue
        if _rejects_as_accessory(product, identity_tokens):
            continue
        if feature_tokens and not product_matches_feature_tokens(product, feature_tokens):
            continue
        movement_ok = product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
            interpretation.preferences.attributes,
        )
        if not movement_ok and not allow_movement_mismatch:
            continue
        color_ok = (
            not color_tokens
            or product_matches_color_tokens(product, color_tokens)
        )
        if require_color and color_tokens and not color_ok:
            continue
        score = 0
        score += 20 * len(identity_tokens)
        if model_codes and any(code in text for code in model_codes):
            score += 50
        if color_ok and color_tokens:
            score += 30
        if feature_tokens and product_matches_feature_tokens(product, feature_tokens):
            score += 40
        if "pulseira_integrada" in feature_tokens:
            if "prx" in text or "integrad" in text:
                score += 55
            elif any(token in text for token in ("couro", "leather", "silicone")):
                score -= 35
        if "acabamento_escovado" in feature_tokens:
            brushed_hit = any(
                token in text for token in ("escovad", "rajad", "brushed", "prata")
            )
            if brushed_hit:
                score += 45
            color_fold = _fold(interpretation.preferences.color)
            if (
                color_fold not in {"preto", "black"}
                and "preto" in text
                and "prata" not in text
                and not brushed_hit
            ):
                score -= 30
        ask_fold = _fold(interpretation.subject.model)
        if "prospex" in ask_fold and (
            is_prospex_diver_ask(interpretation.subject.model)
            or "samurai" in ask_fold
            or "turtle" in ask_fold
        ):
            if "sea samurai" in text or ("samurai" in text and "prospex" in text):
                score += 60
            elif "king turtle" in text or ("turtle" in text and "prospex" in text):
                score += 45
        if case_tokens:
            # Soft: silver/steel case should outrank all-black-case siblings when
            # dial color alone collides on "preto". Gold case is a hard preference
            # signal ("dourado + visor preto") — boost strongly or demote rivals.
            case_ok = product_matches_case_finish_tokens(product, case_tokens)
            goldish = bool({"dourado", "gold", "golden", "ouro"} & set(case_tokens))
            if case_ok:
                score += 45 if goldish else 15
            elif goldish:
                score -= 40
            elif {"prata", "aco", "steel"} & set(case_tokens) and "preto" in text:
                # Title says Preto (often dial) but finish asked for steel —
                # mild penalty vs Samurai/steel-titled siblings.
                score -= 10
        if movement_ok:
            score += 10
        elif model_excludes_gmt(interpretation.subject.model) and "gmt" in text:
            score -= 5
        if any(
            product.get(key) not in (None, "", 0, "0")
            for key in ("current_price", "promotional_price", "price")
        ):
            score += 5
        scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    return [product for _, product in scored[: max(1, limit)]]


def prefer_dial_and_case_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = CUSTOMER_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """When dial + case finish are both known, surface conjunction matches first."""
    color_tokens = preference_color_tokens(interpretation)
    case_tokens = preference_case_finish_tokens(interpretation)
    if not color_tokens or not case_tokens:
        return products[:limit]
    both = [
        product
        for product in products
        if product_matches_color_tokens(product, color_tokens)
        and product_matches_case_finish_tokens(product, case_tokens)
        and not product_conflicts_dial_color(product, color_tokens)
    ]
    if both:
        return score_catalog_candidates(
            both,
            interpretation,
            require_color=True,
            allow_movement_mismatch=False,
            limit=limit,
        ) or both[:limit]
    return products[:limit]


def keyword_match_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    require_color: bool = False,
) -> list[dict[str, Any]]:
    """Compatibility wrapper around score_catalog_candidates."""
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=require_color,
        allow_movement_mismatch=False,
        limit=max(RERANK_SELECTION_LIMIT, CUSTOMER_RESULT_LIMIT),
    )


def specific_product_search_terms(
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    subject = interpretation.subject
    preferences = interpretation.preferences
    values = (
        subject.model,
        subject.product_type,
        preferences.style,
        preferences.color,
        preferences.material,
        *preferences.attributes,
    )
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _fold(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return tuple(terms)


def _term_tokens(terms: tuple[str, ...]) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for token in re.findall(r"[a-z0-9]+", term):
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _evenly_spaced_candidates(
    products: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if len(products) <= limit:
        return products
    if limit <= 1:
        return products[:limit]
    indexes = {
        round(index * (len(products) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [products[index] for index in sorted(indexes)]


def prefilter_specific_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = SEMANTIC_MATCH_POOL_LIMIT,
) -> list[dict[str, Any]]:
    """Reduce real catalog candidates without deciding semantic correctness."""
    terms = specific_product_search_terms(interpretation)
    tokens = _term_tokens(terms)
    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    unscored: list[dict[str, Any]] = []
    for index, product in enumerate(products):
        text = _product_text(product)
        phrase_matches = sum(1 for term in terms if term in text)
        token_matches = sum(1 for token in tokens if token in text)
        if phrase_matches or token_matches:
            scored.append((phrase_matches, token_matches, index, product))
        else:
            unscored.append(product)
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    selected = [product for _, _, _, product in scored[:limit]]
    if len(selected) < limit:
        selected_ids = {
            str(product.get("id"))
            for product in selected
            if product.get("id") is not None
        }
        remaining = [
            product
            for product in unscored
            if product.get("id") is None
            or str(product.get("id")) not in selected_ids
        ]
        selected.extend(
            _evenly_spaced_candidates(remaining, limit - len(selected))
        )
    return selected[:limit]


def semantic_preferences(interpretation: SalesInterpretation) -> dict[str, Any]:
    preferences = interpretation.preferences
    gender = None
    try:
        from app.catalog.specs.preference_normalize import preference_gender_label

        gender = preference_gender_label(interpretation)
    except Exception as exc:
        from app.catalog.retrieval.runtime import log_swallowed

        log_swallowed("scoring.gender_label", exc)
        gender = preferences.recipient
    return {
        key: value
        for key, value in {
            "style": preferences.style,
            "color": preferences.color,
            "material": preferences.material,
            "occasion": preferences.occasion,
            "recipient": preferences.recipient,
            "gender": gender,
            "attributes": preferences.attributes,
            "explicit_no_preferences": preferences.explicit_no_preferences,
        }.items()
        if value not in (None, [], "")
    }


def _compact_property_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return str(value)[:160]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [
            _compact_property_evidence(item, depth=depth + 1)
            for item in value[:12]
        ]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _compact_property_evidence(item, depth=depth + 1)
            for key, item in list(value.items())[:20]
        }
    return str(value)[:160]


def compact_candidates(
    products: list[dict[str, Any]],
    *,
    limit: int = CANDIDATE_POOL_LIMIT,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for product in products[: max(1, limit)]:
        compact.append({
            key: value
            for key, value in {
                "id": str(product.get("id")) if product.get("id") is not None else None,
                "name": product.get("name"),
                "brand": product.get("brand"),
                "model": product.get("model"),
                "category": product.get("category"),
                "category_name": product.get("category_name"),
                "category_id": product.get("category_id"),
                "related_categories": product.get("related_categories"),
                "description": str(product.get("description") or "")[:240] or None,
                "properties": _compact_property_evidence(
                    product.get("properties") or product.get("attributes")
                ),
                "color": product.get("color"),
                "style": product.get("style"),
                "material": product.get("material"),
                "price": product.get("price"),
                "promotional_price": product.get("promotional_price"),
                "current_price": product.get("current_price"),
                "availability": product.get("availability"),
                "available": product.get("available"),
                "available_in_store": product.get("available_in_store"),
                "has_variation": product.get("has_variation"),
                "ProductSettings": product.get("ProductSettings"),
                "variants": _compact_variants(product.get("variants")),
            }.items()
            if value is not None
        })
    return compact


def _brand_compatible_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    expected_brand = _fold(interpretation.subject.brand)
    if not expected_brand:
        return list(products)
    compatible: list[dict[str, Any]] = []
    for product in products:
        candidate_brand = _fold(product.get("brand"))
        if candidate_brand and candidate_brand == expected_brand:
            compatible.append(product)
            continue
        if not candidate_brand and expected_brand in _product_text(product):
            compatible.append(product)
    return compatible


def infer_family_codes_from_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    """Pull shared family codes (C63, C60…) from sibling titles already in the pool."""
    color_tokens = preference_color_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    if not identity_tokens:
        return ()
    codes: list[str] = []
    for product in products:
        text = _product_text(product)
        if not all(token in text for token in identity_tokens):
            continue
        name = str(product.get("name") or "")
        # Prefer short family prefixes (C63). Ignore reference fragments like
        # 39AGM3 that pollute Tray name probes and burn the enrich budget.
        for match in re.findall(r"\b[Cc]\d{2}\b", name):
            codes.append(match.upper())
        for code in extract_model_codes(name):
            if re.fullmatch(r"[Cc]\d{2}", code):
                codes.append(code.upper())
    return tuple(dict.fromkeys(codes))[:3]


def soft_confirm_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = CUSTOMER_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Best catalog near-matches to show for 'é esse da foto?' confirmation."""
    color_tokens = preference_color_tokens(interpretation)
    if color_tokens:
        # Never substitute Kingfisher/Dagger when a dial color was requested.
        return score_catalog_candidates(
            products,
            interpretation,
            require_color=True,
            allow_movement_mismatch=False,
            limit=limit,
        )
    identity_hits = score_catalog_candidates(
        products,
        interpretation,
        require_color=False,
        allow_movement_mismatch=False,
        limit=limit,
    )
    if identity_hits:
        return identity_hits
    # Last resort: allow GMT siblings only when nothing movement-compatible exists.
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=False,
        allow_movement_mismatch=True,
        limit=limit,
    )


def exact_progress_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Matches good enough to stop searching — honors color when requested."""
    color_tokens = preference_color_tokens(interpretation)
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=bool(color_tokens),
        allow_movement_mismatch=False,
        limit=RERANK_SELECTION_LIMIT,
    )


def exact_specific_product_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    subject = interpretation.subject
    candidates = _brand_compatible_candidates(products, interpretation)
    expected_reference = _fold(effective_product_reference(subject.reference))
    expected_ean = _fold(subject.ean)
    expected_model = _fold(subject.model)
    expected_brand_model = _fold(
        " ".join(
            value
            for value in (subject.brand, subject.model)
            if value
        )
    )
    matches: list[dict[str, Any]] = []
    for product in candidates:
        if not product_compatible_with_requested_movement(
            product,
            subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        if expected_reference:
            if _fold(product.get("reference")) == expected_reference:
                matches.append(product)
            continue
        if expected_ean:
            if _fold(product.get("ean")) == expected_ean:
                matches.append(product)
            continue
        if expected_model:
            candidate_model = _fold(product.get("model"))
            candidate_name = _fold(product.get("name"))
            if candidate_model == expected_model or candidate_name in {
                expected_model,
                expected_brand_model,
            }:
                matches.append(product)
                continue
            # Tray often stores short model ("Sealander") while the customer
            # asks with style/color words ("C63 Sealander Automático Rosa").
            # Color/material tokens are optional when identity tokens suffice.
            required = required_model_tokens(subject.model)
            text = _product_text(product)
            if not required or not all(token in text for token in required):
                continue
            if len(required) >= 2:
                matches.append(product)
                continue
            token = required[0]
            if candidate_model in {token, expected_model}:
                matches.append(product)
                continue
            if candidate_model and token not in candidate_model:
                continue
            name_tokens = set(re.findall(r"[a-z0-9]+", candidate_name))
            # Single-token asks ("Explorer") must not match accessories
            # like "Explorer Strap" when the model field is empty.
            if not candidate_model and name_tokens & _ACCESSORY_NAME_TOKENS:
                continue
            if token in candidate_name:
                matches.append(product)
    return matches
