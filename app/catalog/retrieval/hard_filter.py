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
    product_matches_required_feature_groups,
    required_feature_groups,
    required_model_tokens,
    preference_gender_tokens,
    product_matches_gender_tokens,
)


def _required_strap_material(interpretation: SalesInterpretation) -> str | None:
    for item in interpretation.preferences.attributes or []:
        folded = _fold(item)
        if folded.startswith("required_strap_material:"):
            return folded.split(":", 1)[1].strip() or None
    return None


_STRAP_TERMS = r"(?:pulseira|bracelete|bracelet|strap)"
_STRAP_MATERIAL_ALIASES = {
    "aco": ("aco", "steel", "inox", "metal"),
    "couro": ("couro", "leather"),
    "borracha": ("borracha", "rubber"),
    "silicone": ("silicone",),
    "titanio": ("titanio", "titanium"),
}


def product_matches_required_strap_material(
    product: dict[str, Any],
    material: str | None,
) -> bool:
    """Require evidence about the strap, without confusing it with case metal."""
    wanted = _fold(material)
    aliases = _STRAP_MATERIAL_ALIASES.get(wanted, (wanted,))
    text = _product_text(product)
    structured = _fold(
        " ".join(
            str(product.get(key) or "")
            for key in ("strap_type", "strap_material", "bracelet_material")
        )
    )
    if structured and any(alias in structured for alias in aliases):
        return True
    for alias in aliases:
        if re.search(rf"{_STRAP_TERMS}.{{0,40}}\b{re.escape(alias)}\b", text):
            return True
    # Variant labels commonly contain only the option value (for example
    # "Aço inoxidável"). The variant identity makes that evidence strap-specific.
    variant_label = _fold(
        " ".join(
            str(product.get(key) or "")
            for key in ("variant_name", "variant_label", "option_name", "name")
        )
    )
    if product.get("variant_id") and any(alias in variant_label for alias in aliases):
        return True
    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            label = _fold(
                " ".join(
                    str(variant.get(key) or "")
                    for key in (
                        "name", "value", "version", "variation", "option",
                        "options", "attributes", "properties",
                    )
                )
            )
            if any(alias in label for alias in aliases):
                return True
    return False

def hard_filter_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"],
    message_text: str | None = None,
    allow_unknown_features: bool = False,
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
    excluded_catalog_tokens = tuple(
        _fold(token)
        for token in getattr(interpretation, "_excluded_catalog_tokens", [])
        if _fold(token)
    )
    from app.catalog.specs.requirements import technical_requirements, feature_evidence, feature_rules
    requirements = technical_requirements(interpretation)
    mandatory_feature_groups = required_feature_groups(interpretation)
    required_strap_material = _required_strap_material(interpretation)
    # Technical fields have a three-state evidence check above. Do not run an
    # older text-only check again and discard candidates awaiting a live sheet.
    covered_terms = {_fold(term) for rule in feature_rules() if rule["field"] in requirements
                     for term in [rule["value"], *rule["aliases"]]}
    mandatory_feature_groups = tuple(group for group in mandatory_feature_groups
                                    if not all(_fold(term) in covered_terms for term in group))
    for product in products:
        if not isinstance(product, dict) or not product.get("id"):
            continue
        # Accessory descriptions mention watches/movements but are not watches.
        # Reject clear accessory titles before spending calls on technical detail.
        requested_type = _fold(subject.product_type)
        if requested_type in {"relogio", "relogios", "watch", "watches", "relogio de pulso"}:
            title = _fold(product.get("name"))
            if re.match(r"^(?:pulseira|bracelete|strap|bracelet|watch\s*winder|"
                        r"kit\s+(?:de\s+)?(?:reparo|ferramentas)|"
                        r"caixa\s+(?:giratoria|de\s+suporte|organizadora))\b", title):
                continue
        text = _product_text(product)
        if excluded_catalog_tokens and any(
            re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text)
            for token in excluded_catalog_tokens
        ):
            continue
        gender_tokens = preference_gender_tokens(interpretation)
        # A declared catalog style must have evidence, not merely lack a conflict.
        styles = {"aviador": ("aviador", "aviation", "pilot"),
                  "piloto": ("piloto", "pilot", "aviador"),
                  "classico": ("classico", "classic", "dress"),
                  "dress": ("dress", "social", "classico"),
                  "esportivo": ("esportivo", "sport"),
                  "field": ("field", "militar"),
                  "militar": ("militar", "military", "field"),
                  "mergulho": ("mergulho", "diver", "diving")}
        style = _fold(interpretation.preferences.style)
        if style in styles:
            evidence = _fold(str(product.get("style") or "") + " " + text)
            if not any(re.search(r"\b" + re.escape(term) + r"\b", evidence) for term in styles[style]):
                continue
        if gender_tokens and not product_matches_gender_tokens(product, gender_tokens):
            continue
        if mode == "recommendation" and "ready_to_ship" in preferences.attributes:
            from app.catalog.retrieval.availability import commercial_availability_facts
            if commercial_availability_facts(product)["immediate_delivery_supported"] is not True:
                continue
        evidence = feature_evidence(product, requirements) if requirements else None
        if evidence and (evidence["status"] == "mismatch" or (evidence["status"] == "unknown" and not allow_unknown_features)):
            continue
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
        if "mechanism" not in requirements and not product_compatible_with_requested_movement(
            product,
            subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        if mandatory_feature_groups and not product_matches_required_feature_groups(
            product, mandatory_feature_groups
        ):
            continue
        if required_strap_material and not product_matches_required_strap_material(
            product, required_strap_material
        ):
            continue
        color_tokens = preference_color_tokens(interpretation)
        if hard_color:
            color_tokens = tuple(dict.fromkeys((*color_tokens, hard_color)))
        # Color aliases are resolved deterministically. A reranker may be skipped
        # by the call budget, so a confirmed shortlist cannot depend on it to
        # remove products that contradict the customer's stated color.
        require_color = bool(color_tokens or hard_color)
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

    # Diver ask: drop dress/100m false divers when the pool already has true divers.
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
                    selected = filtered
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

        case_range = interpretation_case_size_range(
            interpretation,
            message_text=message_text,
        )
        if case_range and selected:
            from app.sales.contextual_discovery import configuration
            from app.catalog.specs.catalog_specs import extract_case_size_range_from_text
            explicit_size = extract_case_size_range_from_text(" ".join(
                [message_text or "", *interpretation.preferences.attributes]
            ))
            strict_size = bool(configuration() and explicit_size)
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
                selected = in_range
            sized = [
                product
                for product in selected
                if product.get("case_size") or extract_case_size_mm(product)
            ]
            if not in_range and (sized or strict_size):
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

    # Chronograph ask: keep chrono-capable rows when the pool already has them.
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
                selected = chrono_hits
    except Exception as exc:
        log_swallowed("hard_filter.chronograph", exc)
    return selected


def interpretation_without_soft_prefs(
    interpretation: SalesInterpretation,
) -> SalesInterpretation:
    """Drop color/style/occasion so a hard budget/brand filter can recover a pool."""
    prefs = interpretation.preferences
    return interpretation.model_copy(
        update={
            "preferences": prefs.model_copy(
                update={"color": None, "style": None, "occasion": None}
            )
        }
    )


def relax_soft_filters_for_empty_pool(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"] = "recommendation",
    message_text: str | None = None,
) -> list[dict[str, Any]]:
    """Keep budget (and brand, if set). Soften color/style when they emptied the pool."""
    if mode != "recommendation" or not products:
        return []
    prefs = interpretation.preferences
    if not (prefs.color or prefs.style or prefs.occasion):
        return []
    relaxed = interpretation_without_soft_prefs(interpretation)
    hits = hard_filter_products(
        products,
        relaxed,
        mode=mode,
        message_text=message_text,
    )
    if hits:
        print(
            "[sales.hard_filter.relax_soft]",
            {
                "before": 0,
                "after": len(hits),
                "dropped": [
                    key
                    for key, value in (
                        ("color", prefs.color),
                        ("style", prefs.style),
                        ("occasion", prefs.occasion),
                    )
                    if value
                ],
                "kept_brand": bool(interpretation.subject.brand),
                "kept_budget": prefs.budget_max is not None or prefs.budget_min is not None,
            },
        )
    return hits
