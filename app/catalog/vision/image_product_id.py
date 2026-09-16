from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any

import httpx
from openai import APIError
from app.configuration.runtime import ConfigurationUnavailable, message as operator_message, policy
from app.config import get_settings
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.ops.turn_runtime import LLMCallBudgetExceeded
from app.catalog.retrieval.aliases import _FEATURE_SEARCH_ALIASES as _FEATURE_MATCH_ALIASES
from app.catalog.vision.identify import (
    download_image_file,
    identify_product_from_image,
)
from app.catalog.vision.prompt import (
    ImageProductIdentification,
    normalize_feature_label,
)
from app.catalog.retrieval.ports import resolve_compiled_retrieval


def identification_has_catalog_identity(identified: ImageProductIdentification) -> bool:
    """Brand+dial-color alone is too weak for keyword Tray (false siblings)."""
    from app.models import ProductPreferences, ProductSubject
    from app.catalog.product_retrieval import (
        effective_product_reference,
        identity_core_tokens,
        preference_color_tokens,
    )

    if effective_product_reference(identified.reference):
        return True
    features = [
        label
        for label in (normalize_feature_label(item) for item in identified.features)
        if label
    ]
    # Chronograph/diver/etc. give enough signal with brand for a targeted search.
    if any(
        label.casefold() in {"cronógrafo", "cronografo", "mergulho", "gmt"}
        for label in features
    ):
        return True
    model = (identified.model or "").strip()
    if not model:
        return False
    color = (identified.color or "").strip() or None
    probe = SalesInterpretation.model_construct(
        domain="commerce",
        goal="find",
        subject=ProductSubject(product_type="relógio", model=model),
        preferences=ProductPreferences(color=color),
        references_previous_context=False,
        needs_clarification=False,
        confidence=float(identified.confidence or 0.0),
    )
    color_tokens = preference_color_tokens(probe)
    core = identity_core_tokens(model, color_tokens=color_tokens)
    if not core:
        return False
    # model="Preto" alone is not identity — identity_core may fall back to the hue.
    from app.catalog.product_retrieval import _DIAL_COLOR_TOKENS, _OPTIONAL_MODEL_TOKENS

    non_color = [
        token
        for token in core
        if token not in color_tokens
        and token not in _DIAL_COLOR_TOKENS
        and token not in _OPTIONAL_MODEL_TOKENS
    ]
    return bool(non_color)


def image_search_eligible(message: IncomingMessage) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "agent_image_search_enabled", True)):
        return False
    if not (message.image_url or "").strip():
        return False
    attachment = (message.attachment_type or "").lower()
    modality = (message.input_modality or "").lower()
    if attachment == "image":
        return True
    return modality in {"image", "text_with_image"}


def interpretation_from_identification(
    identified: ImageProductIdentification,
) -> SalesInterpretation:
    from app.catalog.product_retrieval import effective_product_reference, normalize_pt_catalog_query

    color = (identified.color or "").strip() or None
    case_finish = (identified.case_finish or "").strip() or None
    raw_reference = (identified.reference or "").strip() or None
    reference = effective_product_reference(raw_reference)
    # Vision sometimes puts dial color phrases into reference.
    if raw_reference and reference is None:
        if not color:
            color = raw_reference
        elif color.casefold() not in raw_reference.casefold():
            color = f"{color} {raw_reference}".strip()

    features = []
    seen_features: set[str] = set()
    for item in list(identified.features or []) + [
        identified.model,
        identified.notes,
    ]:
        label = normalize_feature_label(item)
        if not label:
            continue
        key = label.casefold()
        if key in seen_features:
            continue
        seen_features.add(key)
        features.append(label)

    model = (identified.model or "").strip() or None
    if model:
        model = normalize_pt_catalog_query(model)
        # Append dial color only when model already has identity (never model="Preto").
        if color and color.casefold() not in model.casefold():
            model = f"{model} {color}".strip()
        model_fold = model.casefold()
        for feature in features:
            feature_fold = feature.casefold()
            if feature_fold in model_fold:
                continue
            # Keep chrono/GMT in the model string for probes. Do NOT append
            # Mergulho onto Prospex Sea Samurai — Tray titles omit "Diver's 200m".
            if feature_fold in {"cronógrafo", "cronografo", "gmt"}:
                model = f"{model} {feature}".strip()
                model_fold = model.casefold()
            elif feature_fold == "mergulho" and not any(
                token in model_fold for token in ("samurai", "turtle", "prospex")
            ):
                model = f"{model} {feature}".strip()
                model_fold = model.casefold()

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "relógio",
            "brand": (identified.brand or "").strip() or None,
            "model": model,
            "reference": reference,
        },
        preferences={
            "color": color,
            "material": case_finish,
            "attributes": features,
        },
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=True,
        needs_clarification=False,
        clarification_question=None,
        confidence=float(identified.confidence or 0.0),
        active_topic="product_search",
        purchase_stage="discovery",
    )
    interpretation._source = "image_vision"
    interpretation._excluded_catalog_tokens = _visible_feature_exclusions(identified)
    return interpretation


def _fold_visible_text(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "")).lower()
        if not unicodedata.combining(char)
    )


def _visible_feature_exclusions(
    identified: ImageProductIdentification,
) -> list[str]:
    """Return operator-defined candidate tokens contradicted by a clear photo."""
    try:
        threshold = float(policy("imageVisibleFeatureExclusionMinConfidence"))
        raw_rules = policy("imageVisibleFeatureExclusionRules")
        rules = json.loads(raw_rules) if isinstance(raw_rules, str) else raw_rules
    except (ConfigurationUnavailable, TypeError, ValueError):
        return []
    if float(identified.confidence or 0.0) < threshold or not isinstance(rules, list):
        return []
    evidence = _fold_visible_text(
        " ".join(
            str(item or "")
            for item in [identified.model, identified.notes, *(identified.features or [])]
        )
    )
    excluded: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        detected = [
            _fold_visible_text(alias)
            for alias in rule.get("detectedAliases", [])
            if str(alias or "").strip()
        ]
        if detected and any(alias in evidence for alias in detected):
            continue
        excluded.extend(
            _fold_visible_text(alias)
            for alias in rule.get("candidateAliases", [])
            if str(alias or "").strip()
        )
    return list(dict.fromkeys(token for token in excluded if token))


def soft_line_interpretation_from_identification(
    identified: ImageProductIdentification,
) -> SalesInterpretation:
    """Relax exact photo match into a line/brand nearby search (Carrera, Sky Pilot…)."""
    from app.catalog.product_retrieval import identity_core_tokens, preference_color_tokens

    base = interpretation_from_identification(identified)
    color_tokens = preference_color_tokens(base)
    core = identity_core_tokens(base.subject.model, color_tokens=color_tokens)
    drop = {
        "cronografo",
        "chronograph",
        "automatico",
        "quartz",
        "eco",
        "drive",
        "ecodrive",
        "multifuncao",
        "alarme",
        *color_tokens,
        "prateado",
        "prata",
        "silver",
    }
    line_tokens = [token for token in core if token not in drop][:3]
    line_label = " ".join(line_tokens).title() if line_tokens else None
    if not line_label:
        raw = (identified.model or "").strip()
        line_label = raw.split()[0] if raw else None
    # Keep dial hue on the probe model (Sealander Rosa) so Tray tokens/name
    # search can lock color — soft mode still avoids hard exact failure.
    color_label = (base.preferences.color or "").strip()
    if color_label and line_label and color_label.casefold() not in line_label.casefold():
        # One dial word only (rosa), not "rosa claro (mostrador)".
        hue = color_label.split()[0]
        probe_model = f"{line_label} {hue}".strip()
    else:
        probe_model = line_label
    soft = base.model_copy(
        update={
            "goal": "recommend",
            "subject": base.subject.model_copy(
                update={
                    "model": probe_model,
                    "reference": None,
                }
            ),
            "references_previous_context": True,
            "active_topic": "nearby_line_options",
        }
    )
    soft._source = "image_vision_soft_line"
    soft._force_recommendation_mode = True
    return soft


def color_locked_line_interpretation(
    identified: ImageProductIdentification,
) -> SalesInterpretation:
    """Stage C/D: force line + dial color tokens (sealander + rosa)."""
    soft = soft_line_interpretation_from_identification(identified)
    soft._source = "image_vision_color_lock"
    return soft


def select_products_for_identified_dial(
    products: list[dict[str, Any]],
    identified: ImageProductIdentification,
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """OCR double-check: keep only catalog rows compatible with Vision dial hue."""
    from app.catalog.product_retrieval import rank_products_for_dial_color

    interpretation = interpretation_from_identification(identified)
    return rank_products_for_dial_color(
        products,
        interpretation,
        limit=limit,
    )

def _nearby_line_preferences(identified: ImageProductIdentification) -> dict[str, Any]:
    soft = soft_line_interpretation_from_identification(identified)
    return {
        "nearby_line_brand": soft.subject.brand,
        "nearby_line_model": soft.subject.model,
        "nearby_line_color": soft.preferences.color,
        "image_identify": identified.model_dump(mode="json"),
    }


def _clarification_result(
    *,
    reason: str,
    identified: ImageProductIdentification | None = None,
) -> AgentResult:
    if identified and not identified.is_watch:
        text = operator_message("image_not_watch")
    elif identified and (identified.brand or identified.model):
        hint = " ".join(
            part
            for part in (identified.brand, identified.model)
            if part
        ).strip()
        text = operator_message("image_identity_uncertain_with_hint", hint=hint)
    else:
        text = operator_message("image_identity_uncertain")
    return AgentResult(
        reply_text=text,
        intent="commerce",
        handoff_required=False,
        safety_reason=reason,
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


def _visual_candidates_result(
    products: list[dict[str, Any]],
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult:
    from app.commerce.commerce_router import _product_lines

    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(products, compact=True), start=1)
    ]
    reply = (
        "Pela foto, estes parecem os mais próximos no catálogo:\n"
        + "\n".join(numbered_lines[:2])
        + "\n\nÉ algum desses?"
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="visual_nearest_neighbor",
        commercial_data={
            "products": products,
            "match_status": "ambiguous",
        },
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "visual_search": True,
            "visual_trigger": trigger,
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


async def _try_visual_fallback(
    message: IncomingMessage,
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult | None:
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        return None
    if not str(getattr(settings, "database_url", "") or "").strip():
        return None

    from app.catalog.vision.product_image_index import (
        caption_from_identification,
        visual_search_from_caption,
        visual_search_from_image_url,
    )

    products: list[dict[str, Any]] = []
    try:
        caption = ""
        if identified is not None:
            caption = caption_from_identification(identified)
        if caption:
            products = await visual_search_from_caption(caption)
        if not products and (message.image_url or "").strip():
            products = await visual_search_from_image_url(str(message.image_url).strip())
    except (
        APIError,
        LLMCallBudgetExceeded,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as exc:
        print("[sales.image.visual.fallback.error]", {
            "trigger": trigger,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        return None

    if not products:
        return None

    top_k = int(getattr(settings, "agent_visual_top_k", 3) or 3)
    selected = products[: max(1, top_k)]
    print("[sales.image.visual.fallback]", {
        "trigger": trigger,
        "match_count": len(selected),
    })
    return _visual_candidates_result(
        selected,
        identified=identified,
        trigger=trigger,
    )


def products_match_required_features(
    products: list[dict[str, Any]],
    features: list[str],
) -> bool:
    """True when every distinctive feature has evidence in at least one product."""
    required: list[tuple[str, ...]] = []
    for item in features:
        label = normalize_feature_label(item)
        if not label:
            continue
        folded = "".join(
            char
            for char in unicodedata.normalize("NFKD", label).lower()
            if not unicodedata.combining(char)
        )
        aliases = _FEATURE_MATCH_ALIASES.get(folded)
        if aliases:
            required.append(aliases)
    if not required:
        return True
    if not products:
        return False
    for aliases in required:
        found = False
        for product in products:
            text = " ".join(
                str(product.get(key) or "")
                for key in ("name", "model", "reference", "description", "attributes")
            )
            folded_text = "".join(
                char
                for char in unicodedata.normalize("NFKD", text).lower()
                if not unicodedata.combining(char)
            )
            if any(alias in folded_text for alias in aliases):
                found = True
                break
        if not found:
            return False
    return True


def _product_id(product: dict[str, Any]) -> str | None:
    value = product.get("id") or product.get("product_id")
    return str(value) if value is not None else None


def filter_products_to_interpretation_family(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Keep candidates that share brand + model identity with the Vision ask."""
    from app.catalog.product_retrieval import (
        identity_core_tokens,
        preference_color_tokens,
        product_compatible_with_requested_movement,
        product_matches_feature_tokens,
        preference_feature_tokens,
        _fold,
        _product_text,
    )

    brand = _fold(interpretation.subject.brand)
    color_tokens = preference_color_tokens(interpretation)
    core = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    feature_tokens = preference_feature_tokens(interpretation)
    excluded_tokens = tuple(
        _fold(token)
        for token in getattr(interpretation, "_excluded_catalog_tokens", [])
        if _fold(token)
    )
    kept: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        text = _product_text(product)
        if excluded_tokens and any(
            re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text)
            for token in excluded_tokens
        ):
            continue
        candidate_brand = _fold(product.get("brand"))
        if brand:
            if candidate_brand and candidate_brand != brand:
                continue
            if not candidate_brand and brand not in text:
                continue
        if core and not all(token in text for token in core):
            continue
        if not product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        if feature_tokens and not product_matches_feature_tokens(product, feature_tokens):
            continue
        kept.append(product)
    return kept


def merge_tray_with_visual_neighbors(
    tray_products: list[dict[str, Any]],
    visual_products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Prefer visual nearest neighbors within the same model family."""
    family_visual = filter_products_to_interpretation_family(
        visual_products,
        interpretation,
    )
    family_tray = filter_products_to_interpretation_family(
        tray_products,
        interpretation,
    )
    if not family_visual and not family_tray:
        return []

    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _add(product: dict[str, Any]) -> None:
        product_id = _product_id(product)
        if not product_id:
            return
        if product_id not in by_id:
            order.append(product_id)
            by_id[product_id] = product
            return
        # Keep visual_distance when present.
        existing = by_id[product_id]
        if product.get("visual_distance") is not None and existing.get(
            "visual_distance"
        ) is None:
            merged = dict(existing)
            merged["visual_distance"] = product.get("visual_distance")
            by_id[product_id] = merged

    # Visual family first (true photo similarity), then tray leftovers.
    for product in family_visual:
        _add(product)
    for product in family_tray:
        _add(product)

    ranked = [by_id[product_id] for product_id in order]
    # If visual distances exist, stable-sort by distance among known ones.
    with_distance = [
        product for product in ranked if product.get("visual_distance") is not None
    ]
    without = [
        product for product in ranked if product.get("visual_distance") is None
    ]
    with_distance.sort(key=lambda item: float(item.get("visual_distance") or 99))
    return (with_distance + without)[: max(1, limit)]


async def _disambiguate_with_visual(
    message: IncomingMessage,
    *,
    identified: ImageProductIdentification,
    interpretation: SalesInterpretation,
    tray_products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Re-rank / replace Tray siblings using visual nearest neighbors."""
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return filtered, None
    if not str(getattr(settings, "database_url", "") or "").strip():
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return filtered, None
    image_url = (message.image_url or "").strip()
    if not image_url:
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return filtered, None

    try:
        from app.catalog.vision.product_image_index import visual_search_from_image_url

        visual_products = await visual_search_from_image_url(image_url)
    except (
        APIError,
        LLMCallBudgetExceeded,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as exc:
        print("[sales.image.visual.disambiguate.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return filtered, None

    if not visual_products:
        filtered = filter_products_to_interpretation_family(tray_products, interpretation)
        return filtered, None

    merged = merge_tray_with_visual_neighbors(
        tray_products,
        visual_products,
        interpretation,
        limit=2,
    )
    print("[sales.image.visual.disambiguate]", {
        "tray_count": len(tray_products),
        "visual_count": len(visual_products),
        "merged_ids": [_product_id(item) for item in merged],
        "best_distance": merged[0].get("visual_distance") if merged else None,
    })
    return merged, "image_visual_disambiguate"


async def handle_image_product_search(
    message: IncomingMessage,
) -> AgentResult | None:
    """Identify a watch from an inbound image and search the Tray catalog."""
    if not image_search_eligible(message):
        return None

    settings = get_settings()
    min_confidence = float(
        getattr(settings, "agent_image_search_min_confidence", 0.55)
    )
    try:
        identified = await identify_product_from_image(message)
    except Exception as exc:
        from app.llm.openai_errors import OpenAIGatewayError

        if not isinstance(
            exc,
            (
                APIError,
                OpenAIGatewayError,
                LLMCallBudgetExceeded,
                httpx.HTTPError,
                ValueError,
                RuntimeError,
            ),
        ):
            raise
        from app.ops.observability import log_exception

        log_exception(
            "sales.image.identify.error",
            exc,
            {
                "image_url_present": bool((message.image_url or "").strip()),
                "image_mime_type": message.image_mime_type,
                "attachment_type": message.attachment_type,
            },
        )
        visual = await _try_visual_fallback(
            message,
            identified=None,
            trigger="image_identify_failed",
        )
        if visual is not None:
            return visual
        return AgentResult(
            reply_text=operator_message("image_analysis_unavailable"),
            intent="commerce",
            handoff_required=False,
            safety_reason="image_identify_failed",
            response_metadata={"domain": "commerce", "image_search": True},
        )

    if not identified.is_watch:
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    low_confidence = (
        float(identified.confidence or 0.0) < min_confidence
        or not any(
            (
                identified.brand,
                identified.model,
                identified.reference,
                identified.features,
            )
        )
    )
    if low_confidence:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_identify_low_confidence",
        )
        if visual is not None:
            return visual
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    # One identity authority for every photographed product. Neither text search
    # nor a generic response reviewer may turn a hypothesis into a confirmed SKU.
    from app.catalog.vision.catalog_evidence import resolve_catalog_photo
    return await resolve_catalog_photo(message, identified)
