from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings
from .models import SalesInterpretation
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


PRODUCT_PAGE_LIMIT = 20
CATALOG_DISCOVERY_MAX_PAGES = 5
CATALOG_DISCOVERY_MAX_PRODUCTS = 100
SEMANTIC_MATCH_POOL_LIMIT = 20
CANDIDATE_POOL_LIMIT = SEMANTIC_MATCH_POOL_LIMIT
CUSTOMER_RESULT_LIMIT = 3
RERANK_SELECTION_LIMIT = 5
MAX_VARIANT_PRODUCT_QUERIES = 5

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ProductRerankSelection(BaseModel):
    selected_product_ids: list[str]


class ProductMatchSelection(BaseModel):
    match_status: Literal["exact", "ambiguous", "none"]
    candidate_ids: list[str]
    best_candidate_id: str | None
    confidence: float


class ProductMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpecificProductResolution:
    status: Literal["exact", "ambiguous", "none"]
    products: tuple[dict[str, Any], ...]
    match_source: Literal["exact", "openai"]
    invalid_ids_count: int = 0


@dataclass(frozen=True)
class CommercialPriceResolution:
    amount: Decimal | None
    source: str | None


@dataclass(frozen=True)
class ProductRetrievalRequest:
    strategy: str
    name: str | None = None
    brand: str | None = None
    reference: str | None = None
    ean: str | None = None
    category_id: str | None = None
    query: str | None = None
    available: bool | None = None
    available_in_store: bool | None = None
    limit: int = CANDIDATE_POOL_LIMIT
    page: int = 1

    def tool_arguments(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "query": self.query,
                "name": self.name,
                "brand": self.brand,
                "reference": self.reference,
                "ean": self.ean,
                "category_id": self.category_id,
                "available": self.available,
                "available_in_store": self.available_in_store,
                "limit": self.limit,
                "page": self.page,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class ProductRetrievalPlan:
    mode: Literal["exact", "recommendation"]
    requests: tuple[ProductRetrievalRequest, ...]
    candidate_limit: int = CANDIDATE_POOL_LIMIT
    customer_result_limit: int = CUSTOMER_RESULT_LIMIT
    discovery_page_limit: int = PRODUCT_PAGE_LIMIT
    discovery_max_pages: int = CATALOG_DISCOVERY_MAX_PAGES
    discovery_max_products: int = CATALOG_DISCOVERY_MAX_PRODUCTS


def _fold(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    ).strip()


_MODEL_STOPWORDS = frozenset(
    {
        "relogio",
        "watch",
        "automatico",
        "automatic",
        "quartz",
        "cronografo",
        "chronograph",
        "mm",
        "com",
        "para",
        "the",
        "and",
    }
)
# Color/material adjectives are useful but must not block identity matches.
_OPTIONAL_MODEL_TOKENS = frozenset(
    {
        "branco",
        "preto",
        "rosa",
        "azul",
        "verde",
        "dourado",
        "prata",
        "cinza",
        "vermelho",
        "amarelo",
        "laranja",
        "bege",
        "claro",
        "escuro",
        "titanio",
        "aco",
        "ouro",
        "couro",
        "borracha",
        "carbon",
        "carbono",
        "ceramica",
        "nylon",
        "pulseira",
        "mostrador",
        "dial",
        "bezel",
    }
)
# Soft descriptors that must never block a match even for single-token models.
_DESCRIPTOR_MODEL_TOKENS = frozenset(
    {
        "claro",
        "escuro",
        "mostrador",
        "dial",
        "bezel",
        "face",
        "caixa",
    }
)
_ACCESSORY_NAME_TOKENS = frozenset(
    {
        "strap",
        "pulseira",
        "caixa",
        "box",
        "kit",
        "tool",
        "capa",
        "case",
        "fone",
        "cabo",
        "adapter",
        "adaptador",
    }
)
_REFERENCE_CODE_RE = re.compile(
    r"\b("
    r"[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,}"
    r"|"
    r"[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){2,}"
    r")\b",
    re.IGNORECASE,
)
# Alphanumeric model codes with digits: PH2000M, C63, SUB300, etc.
_MODEL_CODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{3,}\b"
)


def significant_model_tokens(model: str | None) -> tuple[str, ...]:
    """Core tokens for exact matching — drops filler words like 'automático'."""
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _fold(model))
        if token and token not in _MODEL_STOPWORDS and len(token) >= 2
    ]
    if tokens:
        return tuple(dict.fromkeys(tokens))
    fallback = [token for token in re.findall(r"[a-z0-9]+", _fold(model)) if token]
    return tuple(dict.fromkeys(fallback))


def required_model_tokens(model: str | None) -> tuple[str, ...]:
    """Identity tokens for matching; color/material are optional when possible."""
    tokens = significant_model_tokens(model)
    identity = tuple(
        token for token in tokens if token not in _OPTIONAL_MODEL_TOKENS
    )
    if len(identity) >= 2 or any(re.search(r"\d", token) for token in identity):
        return identity
    # For single-word models (Sealander), keep color tokens for ranking/disambiguation
    # but never require dial descriptors invented by Vision ("claro", "mostrador").
    tightened = tuple(
        token for token in tokens if token not in _DESCRIPTOR_MODEL_TOKENS
    )
    return tightened or identity or tokens


def known_family_model_codes(brand: str | None, model: str | None) -> tuple[str, ...]:
    """Catalog family codes Vision often misses (e.g. C63 on Sealander dials)."""
    brand_fold = _fold(brand)
    model_fold = _fold(model)
    if not brand_fold or not model_fold:
        return ()
    codes: list[str] = []
    if "christopher ward" in brand_fold and "sealander" in model_fold:
        codes.append("C63")
    return tuple(codes)


def identity_core_tokens(
    model: str | None,
    *,
    color_tokens: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Model tokens for Tray probes — never mix hue words into the core query."""
    required = required_model_tokens(model)
    drop = set(color_tokens) | _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS
    core = tuple(token for token in required if token not in drop)
    if core:
        return core
    # Fall back to non-color identity even for single-token models.
    identity = tuple(
        token
        for token in significant_model_tokens(model)
        if token not in drop
    )
    return identity or required


def keyword_match_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    require_color: bool = False,
) -> list[dict[str, Any]]:
    """Match catalog rows by keyword overlap (brand/model/color tokens in title)."""
    color_tokens = preference_color_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    brand_fold = _fold(interpretation.subject.brand)
    matches: list[dict[str, Any]] = []
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
        if not product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
        ):
            continue
        if require_color and color_tokens:
            if not product_matches_color_tokens(product, color_tokens):
                continue
        matches.append(product)
    return matches


def is_plausible_product_reference(value: str | None) -> bool:
    """True when value looks like a commercial SKU/ref, not a color description."""
    text = str(value or "").strip()
    if not text:
        return False
    if extract_reference_code(text):
        return True
    folded = _fold(text)
    tokens = [token for token in re.findall(r"[a-z0-9]+", folded) if token]
    if not tokens:
        return False
    soft = _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS | _MODEL_STOPWORDS
    if all(token in soft for token in tokens):
        return False
    # Real refs almost always mix letters/digits with separators.
    if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", text) and re.search(
        r"[\.\-_/]",
        text,
    ):
        return len(text) >= 6
    return False


def effective_product_reference(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if is_plausible_product_reference(text):
        return extract_reference_code(text) or text
    return None


def normalize_pt_catalog_query(text: str | None) -> str:
    """Map common English vision terms to Tray catalog Portuguese."""
    value = str(text or "").strip()
    if not value:
        return ""
    replacements = (
        (r"\bautomatic\b", "Automático"),
        (r"\bchronograph\b", "Cronógrafo"),
        (r"\bquartz\b", "Quartz"),
        (r"\bpink\b", "Rosa"),
        (r"\bblue\b", "Azul"),
        (r"\bgreen\b", "Verde"),
        (r"\bblack\b", "Preto"),
        (r"\bwhite\b", "Branco"),
    )
    updated = value
    for pattern, replacement in replacements:
        updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)
    return updated


def preference_color_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    color = _fold(interpretation.preferences.color)
    if not color:
        # Recover color adjectives embedded in the model string from Vision.
        color = " ".join(
            token
            for token in significant_model_tokens(interpretation.subject.model)
            if token in _OPTIONAL_MODEL_TOKENS
            and token not in _DESCRIPTOR_MODEL_TOKENS
        )
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", color)
        if token and token not in _DESCRIPTOR_MODEL_TOKENS
    ]
    return tuple(dict.fromkeys(tokens))


def product_matches_color_tokens(
    product: dict[str, Any],
    color_tokens: tuple[str, ...],
) -> bool:
    if not color_tokens:
        return True
    text = _product_text(product)
    return all(token in text for token in color_tokens)


def model_excludes_gmt(model: str | None) -> bool:
    folded = _fold(model)
    if not folded:
        return False
    if "gmt" in folded:
        return False
    return "automatic" in folded or "automatico" in folded


def product_compatible_with_requested_movement(
    product: dict[str, Any],
    model: str | None,
) -> bool:
    if not model_excludes_gmt(model):
        return True
    text = _product_text(product)
    # Customer/Vision asked Automatic without GMT — don't substitute GMT siblings.
    return "gmt" not in text


def extract_reference_code(text: str | None) -> str | None:
    match = _REFERENCE_CODE_RE.search(str(text or ""))
    return match.group(1).strip() if match else None


def extract_model_codes(text: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_MODEL_CODE_RE.findall(str(text or ""))))


def _product_text(product: dict[str, Any]) -> str:
    fields = (
        "name", "brand", "model", "reference", "ean", "description",
        "category", "category_name", "category_id", "attributes", "color",
        "style", "material", "properties", "ProductSettings", "variants",
    )
    return _fold(" ".join(str(product.get(field) or "") for field in fields))


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


class ProductRetrievalCompiler:
    @staticmethod
    def compile(
        interpretation: SalesInterpretation,
        *,
        category_ids: tuple[str, ...] | list[str] = (),
    ) -> ProductRetrievalPlan:
        subject = interpretation.subject
        inferred_reference = effective_product_reference(subject.reference) or (
            extract_reference_code(
                " ".join(
                    part
                    for part in (subject.model, subject.brand, subject.product_type)
                    if part
                )
            )
        )
        exact = bool(inferred_reference or subject.ean or subject.model)
        requests: list[ProductRetrievalRequest] = []

        if subject.ean:
            requests.append(ProductRetrievalRequest(strategy="exact_ean", ean=subject.ean))
        elif inferred_reference:
            requests.append(
                ProductRetrievalRequest(
                    strategy="exact_reference",
                    reference=inferred_reference,
                )
            )
            if subject.model:
                requests.append(
                    ProductRetrievalRequest(
                        strategy="exact_query_reference_context",
                        query=" ".join(
                            part
                            for part in (subject.brand, subject.model)
                            if part
                        ).strip(),
                    )
                )
        elif subject.model:
            pt_model = normalize_pt_catalog_query(subject.model)
            color_tokens = preference_color_tokens(interpretation)
            # Use hue only in Tray probes — "claro"/"mostrador" exclude real titles.
            color_label = " ".join(color_tokens).strip()
            core_tokens = identity_core_tokens(
                pt_model or subject.model,
                color_tokens=color_tokens,
            )
            core_query = " ".join(core_tokens[:4]).strip()
            model_codes = tuple(
                dict.fromkeys(
                    (
                        *extract_model_codes(pt_model or subject.model),
                        *known_family_model_codes(subject.brand, pt_model or subject.model),
                    )
                )
            )
            wants_automatic = bool(
                re.search(r"\b(automatic|automatico)\b", _fold(subject.model))
            )
            seen_probe_keys: set[str] = set()

            def _add_name_probe(
                strategy: str,
                name: str | None,
                *,
                brand: str | None = None,
            ) -> None:
                cleaned = " ".join(str(name or "").split()).strip()
                if not cleaned:
                    return
                key = f"name|{_fold(cleaned)}|{_fold(brand)}"
                if key in seen_probe_keys:
                    return
                seen_probe_keys.add(key)
                requests.append(
                    ProductRetrievalRequest(
                        strategy=strategy,
                        name=cleaned,
                        brand=brand,
                    )
                )

            # Color-first probes so "Sealander rosa" beats GMT azul/verde siblings.
            # Keep this list short — probes run in parallel, but Tray still costs time.
            if color_label and core_query:
                auto_bit = "Automático" if wants_automatic else None
                core_label = core_query.title()
                code_bit = model_codes[0] if model_codes else None
                color_hue = color_label.title()
                color_names = [
                    " ".join(part for part in (core_label, color_hue) if part),
                    " ".join(
                        part for part in (core_label, auto_bit, color_hue) if part
                    ),
                    " ".join(
                        part
                        for part in (code_bit, core_label, auto_bit, color_hue)
                        if part
                    ),
                    # Storefront titles insert family code between brand and model:
                    # "Relógio Christopher Ward C63 Sealander Automático Rosa …"
                    " ".join(
                        part
                        for part in (
                            "Relógio",
                            subject.brand,
                            code_bit,
                            core_label,
                            auto_bit,
                            color_hue,
                        )
                        if part
                    ),
                ]
                # Bare family+model phrase that appears contiguously in titles.
                if code_bit and core_label:
                    color_names.insert(
                        0,
                        " ".join(
                            part
                            for part in (code_bit, core_label, auto_bit, color_hue)
                            if part
                        ),
                    )
                for index, name in enumerate(color_names):
                    # Prefer name-only (no brand filter): Tray often fails AND(name, brand).
                    _add_name_probe(f"exact_color_name_{index}", name)
                    if index < 2 and subject.brand:
                        _add_name_probe(
                            f"exact_color_brand_{index}",
                            name,
                            brand=subject.brand,
                        )
                if subject.brand:
                    _add_name_probe(
                        "exact_color_brand_only",
                        color_hue,
                        brand=subject.brand,
                    )
            # Identity probes without Vision filler ("rosa claro (mostrador)").
            if color_label:
                identity_name = " ".join(
                    part
                    for part in (
                        " ".join(model_codes[:1]) if model_codes else None,
                        core_query.title() if core_query else None,
                        "Automático" if wants_automatic else None,
                    )
                    if part
                ).strip() or (pt_model or subject.model)
            else:
                identity_name = pt_model or subject.model
            _add_name_probe(
                "exact_model_with_brand" if subject.brand else "exact_model",
                identity_name,
                brand=subject.brand,
            )
            catalog_title = " ".join(
                part
                for part in (
                    "Relógio",
                    subject.brand,
                    identity_name,
                    color_label.title() if color_label else None,
                )
                if part
            ).strip()
            _add_name_probe("exact_catalog_title", catalog_title)
            # Same title without brand filter — matches storefront search better.
            if subject.brand:
                _add_name_probe("exact_catalog_title_broad", catalog_title)
            brand_model_query = " ".join(
                part for part in (subject.brand, identity_name) if part
            ).strip()
            if brand_model_query and _fold(brand_model_query) != _fold(identity_name or ""):
                requests.append(
                    ProductRetrievalRequest(
                        strategy="exact_query_full",
                        query=brand_model_query,
                    )
                )
            for code in model_codes[:1]:
                _add_name_probe("exact_model_code", code, brand=subject.brand)
                _add_name_probe("exact_model_code_broad", code)
            if subject.brand:
                _add_name_probe(
                    "exact_model_broad",
                    core_query.title() if core_query else identity_name,
                )
                requests.append(ProductRetrievalRequest(
                    strategy="brand_candidates",
                    brand=subject.brand,
                ))
            # Category paging is expensive; only use it when brand discovery
            # isn't available.
            if not subject.brand:
                for category_id in category_ids[:5]:
                    requests.append(ProductRetrievalRequest(
                        strategy="category_candidates",
                        category_id=str(category_id),
                    ))
        else:
            available = True
            available_in_store = True
            for index, category_id in enumerate(category_ids[:5]):
                requests.append(ProductRetrievalRequest(
                    strategy="category" if index == 0 else "category_child",
                    category_id=str(category_id),
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
            if subject.product_type:
                requests.append(ProductRetrievalRequest(
                    strategy="name_fallback",
                    name=subject.product_type,
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
            elif subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="explicit_brand",
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))

        discovery_pages = CATALOG_DISCOVERY_MAX_PAGES
        if preference_color_tokens(interpretation):
            # A few extra brand pages help surface color variants without
            # blowing the Vercel Hobby wall-clock budget.
            discovery_pages = max(discovery_pages, 8)

        return ProductRetrievalPlan(
            mode="exact" if exact else "recommendation",
            requests=tuple(requests),
            discovery_max_pages=discovery_pages,
            discovery_max_products=max(
                CATALOG_DISCOVERY_MAX_PRODUCTS,
                discovery_pages * PRODUCT_PAGE_LIMIT,
            ),
        )


def _decimal_money_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("value", "amount"):
            if key in value:
                return _decimal_money_value(value.get(key))
        return None
    try:
        if isinstance(value, str):
            normalized = value.replace("R$", "").replace("\xa0", " ").strip()
            normalized = normalized.replace(" ", "")
            if "," in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            return Decimal(normalized)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def resolve_commercial_price(
    product: dict[str, Any],
    *,
    require_positive: bool = False,
) -> CommercialPriceResolution:
    first_present_source: str | None = None
    for key in ("current_price", "promotional_price", "price"):
        if key not in product or product.get(key) is None:
            continue
        first_present_source = first_present_source or key
        amount = _decimal_money_value(product.get(key))
        if require_positive and (amount is None or amount <= Decimal("0")):
            continue
        return CommercialPriceResolution(amount=amount, source=key)
    return CommercialPriceResolution(amount=None, source=first_present_source)


def effective_price(product: dict[str, Any]) -> float | None:
    resolved = resolve_commercial_price(product)
    return float(resolved.amount) if resolved.amount is not None else None


def _known_unavailable(product: dict[str, Any]) -> bool:
    availability_fields = (
        product.get("available"),
        product.get("available_in_store"),
        product.get("available_for_purchase"),
    )
    known = [value for value in availability_fields if value is not None]
    if not known:
        return False
    def is_false(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"0", "false", "no", "não"}
        return value is False or value == 0
    return all(is_false(value) for value in known)


def _truth_state(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = _fold(value)
        if normalized in {"1", "true", "yes", "sim"}:
            return True
        if normalized in {"0", "false", "no", "nao"}:
            return False
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def product_availability_state(
    product: dict[str, Any],
) -> Literal["available", "unavailable", "unknown"]:
    for source in (product, product.get("ProductSettings")):
        if not isinstance(source, dict):
            continue
        if _truth_state(source.get("upon_request")) is True:
            return "unavailable"
    values: list[bool] = []
    for key in ("available", "available_in_store", "available_for_purchase"):
        state = _truth_state(product.get(key))
        if state is not None:
            values.append(state)
    settings = product.get("ProductSettings")
    if isinstance(settings, dict):
        for key in ("available", "available_in_store", "available_for_purchase"):
            state = _truth_state(settings.get(key))
            if state is not None:
                values.append(state)
    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for key in ("available", "available_in_store", "available_for_purchase"):
                state = _truth_state(variant.get(key))
                if state is not None:
                    values.append(state)
            variant_settings = variant.get("VariationSettings")
            if isinstance(variant_settings, dict):
                for key in ("available", "available_in_store", "available_for_purchase"):
                    state = _truth_state(variant_settings.get(key))
                    if state is not None:
                        values.append(state)
    if any(values):
        return "available"
    if values and not any(values):
        return "unavailable"
    return "unknown"


def commercial_availability_facts(product: dict[str, Any]) -> dict[str, Any]:
    settings = product.get("ProductSettings")
    settings = settings if isinstance(settings, dict) else {}
    lead_time_value = next(
        (
            source.get(key)
            for source in (product, settings)
            for key in (
                "order_days_availability",
                "lead_time_days",
                "availability_days",
                "delivery_days",
                "lead_time",
            )
            if source.get(key) not in (None, "")
        ),
        None,
    )
    lead_time_days: int | None = None
    if isinstance(lead_time_value, (int, float)) and not isinstance(lead_time_value, bool):
        lead_time_days = max(int(lead_time_value), 0)
    elif isinstance(lead_time_value, str):
        match = re.search(r"\d+", lead_time_value)
        if match:
            lead_time_days = int(match.group(0))

    immediate_flag = next(
        (
            state
            for source in (product, settings)
            for key in ("immediate_delivery", "ready_to_ship")
            if (state := _truth_state(source.get(key))) is not None
        ),
        None,
    )
    if lead_time_days is not None:
        immediate_delivery_supported: bool | None = lead_time_days == 0
    else:
        immediate_delivery_supported = immediate_flag
    stock = product.get("stock")
    return {
        "availability_state": product_availability_state(product),
        "has_stock": stock is not None,
        "stock": stock,
        "has_lead_time": lead_time_days is not None,
        "lead_time_days": lead_time_days,
        "immediate_delivery_supported": immediate_delivery_supported,
    }


def hard_filter_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"],
) -> list[dict[str, Any]]:
    subject = interpretation.subject
    preferences = interpretation.preferences
    expected_brand = _fold(subject.brand)
    expected_model = _fold(subject.model)
    expected_reference = _fold(effective_product_reference(subject.reference))
    expected_ean = _fold(subject.ean)
    selected: list[dict[str, Any]] = []

    for product in products:
        if not isinstance(product, dict) or not product.get("id"):
            continue
        text = _product_text(product)
        if expected_brand:
            candidate_brand = _fold(product.get("brand"))
            if candidate_brand and candidate_brand != expected_brand:
                continue
            if not candidate_brand and expected_brand not in text:
                continue
        if expected_reference and _fold(product.get("reference")) != expected_reference:
            continue
        if expected_ean and _fold(product.get("ean")) != expected_ean:
            continue
        if not product_compatible_with_requested_movement(product, subject.model):
            continue
        color_tokens = preference_color_tokens(interpretation)
        if color_tokens and not product_matches_color_tokens(product, color_tokens):
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
    return selected


def semantic_preferences(interpretation: SalesInterpretation) -> dict[str, Any]:
    preferences = interpretation.preferences
    return {
        key: value
        for key, value in {
            "style": preferences.style,
            "color": preferences.color,
            "material": preferences.material,
            "occasion": preferences.occasion,
            "recipient": preferences.recipient,
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


def compact_candidates(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for product in products[:CANDIDATE_POOL_LIMIT]:
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


def soft_confirm_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = CUSTOMER_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Best catalog near-matches to show for 'é esse da foto?' confirmation."""
    color_hits = keyword_match_products(
        products,
        interpretation,
        require_color=True,
    )
    if color_hits:
        return color_hits[: max(1, limit)]
    identity_hits = keyword_match_products(
        products,
        interpretation,
        require_color=False,
    )
    if identity_hits:
        return identity_hits[: max(1, limit)]
    # Last resort: identity token overlap without movement filter.
    compatible = _brand_compatible_candidates(products, interpretation)
    color_tokens = preference_color_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    scored: list[tuple[int, dict[str, Any]]] = []
    for product in compatible:
        text = _product_text(product)
        if identity_tokens and not all(token in text for token in identity_tokens):
            continue
        score = 0
        if product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
        ):
            score += 10
        elif model_excludes_gmt(interpretation.subject.model) and "gmt" in text:
            score -= 5
        if color_tokens and product_matches_color_tokens(product, color_tokens):
            score += 20
        scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    preferred = [product for score, product in scored if score >= 10]
    pool = preferred or [product for score, product in scored if score >= 0]
    return pool[: max(1, limit)]


def exact_progress_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Matches good enough to stop searching — honors color when the customer asked for one."""
    color_tokens = preference_color_tokens(interpretation)
    if color_tokens:
        keyword_hits = keyword_match_products(
            products,
            interpretation,
            require_color=True,
        )
        if keyword_hits:
            return keyword_hits
    matches = exact_specific_product_matches(products, interpretation)
    if not color_tokens:
        return matches
    return [
        product
        for product in matches
        if product_matches_color_tokens(product, color_tokens)
    ]


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
        if not product_compatible_with_requested_movement(product, subject.model):
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


async def match_specific_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> SpecificProductResolution:
    compatible = _brand_compatible_candidates(products, interpretation)
    color_tokens = preference_color_tokens(interpretation)
    # Keyword overlap first — titles like
    # "Relógio Christopher Ward C63 Sealander Automático Rosa …" should match
    # Vision output "Christopher Ward Sealander Automatic rosa".
    if color_tokens:
        keyword_color = keyword_match_products(
            compatible,
            interpretation,
            require_color=True,
        )
        if keyword_color:
            selected = keyword_color[:RERANK_SELECTION_LIMIT]
            status: Literal["exact", "ambiguous", "none"] = (
                "exact" if len(selected) == 1 else "ambiguous"
            )
            print("[sales.product.match]", {
                "candidate_count": len(compatible),
                "selected_count": len(selected),
                "invalid_ids_count": 0,
                "match_source": "exact",
                "reason": "keyword_color",
            })
            return SpecificProductResolution(
                status=status,
                products=tuple(selected),
                match_source="exact",
            )

    exact_matches = exact_specific_product_matches(compatible, interpretation)
    identity_matches = list(exact_matches)
    if color_tokens:
        exact_matches = [
            product
            for product in exact_matches
            if product_matches_color_tokens(product, color_tokens)
        ]
    if exact_matches:
        selected = exact_matches[:RERANK_SELECTION_LIMIT]
        status = (
            "exact" if len(selected) == 1 else "ambiguous"
        )
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="exact",
        )

    # Identity matched (e.g. Sealander Automatic) but not the requested color.
    # Present those movement-compatible options so the customer can confirm,
    # instead of asking for a SKU reference they don't know.
    if color_tokens and identity_matches:
        selected = identity_matches[:RERANK_SELECTION_LIMIT]
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "color_mismatch_soft_confirm",
        })
        return SpecificProductResolution(
            status="ambiguous",
            products=tuple(selected),
            match_source="exact",
        )
    keyword_identity = keyword_match_products(
        compatible,
        interpretation,
        require_color=False,
    )
    if keyword_identity:
        selected = keyword_identity[:RERANK_SELECTION_LIMIT]
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "keyword_identity",
        })
        return SpecificProductResolution(
            status="ambiguous" if color_tokens or len(selected) > 1 else "exact",
            products=tuple(selected),
            match_source="exact",
        )
    if (
        model_excludes_gmt(interpretation.subject.model)
        and compatible
        and not any(
            product_compatible_with_requested_movement(
                product,
                interpretation.subject.model,
            )
            for product in compatible
        )
    ):
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "movement_mismatch",
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )

    settings = get_settings()
    if not compatible:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": 0,
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )
    if not settings.openai_api_key:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": "OpenAIUnavailable",
        })
        raise ProductMatchError("specific_product_match_unavailable")

    candidate_by_id = {
        str(product["id"]): product
        for product in compatible
        if product.get("id") is not None
    }
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await execute_openai_call(
            call_type="product_selection",
            operation=lambda: client.chat.completions.parse(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Resolva o produto ou modelo pedido usando somente produtos reais de "
                            "CANDIDATES. O nome informado pode ser parcial, aproximado, uma coleção ou "
                            "uma descrição informal. Use match_status=exact somente quando um único "
                            "produto estiver identificado com segurança; use ambiguous quando dois ou "
                            "mais candidatos tiverem relação semântica relevante e não for seguro "
                            "escolher apenas um; use none quando não houver relação suficiente. Marca "
                            "igual, sozinha, não é correspondência. candidate_ids e best_candidate_id "
                            "devem conter exclusivamente IDs presentes em CANDIDATES. Não sugira "
                            "alternativas sem relação com o pedido."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "SUBJECT": interpretation.subject.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                ),
                                "CANDIDATES": compact_candidates(compatible),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format=ProductMatchSelection,
            ),
        )
        parsed = response.choices[0].message.parsed if response.choices else None
        if not isinstance(parsed, ProductMatchSelection):
            raise ValueError("product_match_schema_missing")
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        invalid_ids = 0
        for product_id in parsed.candidate_ids[:RERANK_SELECTION_LIMIT]:
            normalized_id = str(product_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            product = candidate_by_id.get(normalized_id)
            if product is None:
                invalid_ids += 1
                continue
            selected.append(product)
        best_candidate_id = (
            str(parsed.best_candidate_id)
            if parsed.best_candidate_id is not None
            else None
        )
        if best_candidate_id is not None and best_candidate_id not in candidate_by_id:
            invalid_ids += 1
            best_candidate_id = None

        status: Literal["exact", "ambiguous", "none"] = "none"
        if parsed.match_status == "exact":
            exact_id = best_candidate_id
            if exact_id is None and len(selected) == 1:
                exact_id = str(selected[0].get("id"))
            exact_product = candidate_by_id.get(exact_id or "")
            if exact_product is not None:
                selected = [exact_product]
                status = "exact"
            else:
                selected = []
        elif parsed.match_status == "ambiguous" and len(selected) >= 2:
            status = "ambiguous"
        else:
            selected = []
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "match_source": "openai",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": invalid_ids,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="openai",
            invalid_ids_count=invalid_ids,
        )
    except (APIError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": type(exc).__name__,
        })
        raise ProductMatchError("specific_product_match_failed") from exc


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
    candidates = _deterministic_semantic_order(products, interpretation)
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


async def revalidate_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    execute_tool: ToolExecutor,
) -> tuple[list[dict[str, Any]], bool]:
    refreshed: list[dict[str, Any]] = []
    failed = False
    for product in products[:CUSTOMER_RESULT_LIMIT]:
        product_id = product.get("id")
        if product_id is None:
            continue
        result = await execute_tool("get_product", {"product_id": str(product_id)})
        if "error" in result:
            failed = True
            continue
        current = {**product, **result}
        current["commercial_availability"] = commercial_availability_facts(current)
        print("[sales.availability.fact]", {
            "has_stock": current["commercial_availability"]["has_stock"],
            "has_lead_time": current["commercial_availability"]["has_lead_time"],
            "immediate_delivery_supported": current["commercial_availability"]["immediate_delivery_supported"],
        })
        refreshed.append(current)
    if refreshed:
        refreshed = await enrich_product_variants(refreshed, interpretation, execute_tool)
    return refreshed, failed


def _deterministic_semantic_order(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    terms = [
        _fold(value)
        for value in (
            interpretation.preferences.style,
            interpretation.preferences.color,
            interpretation.preferences.material,
            interpretation.preferences.occasion,
            interpretation.preferences.recipient,
            *interpretation.preferences.attributes,
        )
        if value
    ]
    scored = [
        (sum(1 for term in terms if term in _product_text(product)), index, product)
        for index, product in enumerate(products)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [product for _, _, product in scored[:RERANK_SELECTION_LIMIT]]


async def rerank_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    available_products = [
        product for product in products
        if product_availability_state(product) != "unavailable"
    ]
    settings = get_settings()
    fallback = _deterministic_semantic_order(available_products, interpretation)
    if not available_products or not settings.openai_api_key:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
        })
        return fallback

    candidate_by_id = {str(product["id"]): product for product in available_products if product.get("id") is not None}
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await execute_openai_call(
            call_type="product_selection",
            operation=lambda: client.chat.completions.parse(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classifique produtos reais da NewStore conforme as preferências estruturadas. "
                            "Retorne no máximo cinco IDs presentes em CANDIDATES, em ordem de relevância. "
                            "Não invente IDs, produtos nem atributos e use somente evidências dos candidatos."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({
                            "PREFERENCES": semantic_preferences(interpretation),
                            "CANDIDATES": compact_candidates(available_products),
                        }, ensure_ascii=False),
                    },
                ],
                response_format=ProductRerankSelection,
            ),
        )
        parsed = response.choices[0].message.parsed if response.choices else None
        if not isinstance(parsed, ProductRerankSelection):
            raise ValueError("reranker_schema_missing")
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        invalid_ids = 0
        for product_id in parsed.selected_product_ids[:RERANK_SELECTION_LIMIT]:
            normalized_id = str(product_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            product = candidate_by_id.get(normalized_id)
            if product is None:
                invalid_ids += 1
                continue
            selected.append(product)
        if not selected:
            selected = fallback
        print("[sales.reranker]", {
            "source": "openai",
            "candidate_count": len(available_products),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
        })
        return selected
    except (APIError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "error_type": type(exc).__name__,
        })
        return fallback
