from __future__ import annotations

import json
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings
from .models import SalesInterpretation


CANDIDATE_POOL_LIMIT = 20
CUSTOMER_RESULT_LIMIT = 3
RERANK_SELECTION_LIMIT = 5
MAX_VARIANT_PRODUCT_QUERIES = 5

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ProductRerankSelection(BaseModel):
    selected_product_ids: list[str]


class ProductMatchSelection(BaseModel):
    selected_product_ids: list[str]


class ProductMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductRetrievalRequest:
    strategy: str
    name: str | None = None
    brand: str | None = None
    reference: str | None = None
    ean: str | None = None
    category_id: str | None = None
    available: bool | None = None
    available_in_store: bool | None = None
    limit: int = CANDIDATE_POOL_LIMIT
    page: int = 1

    def tool_arguments(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
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


def _fold(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    ).strip()


def _product_text(product: dict[str, Any]) -> str:
    fields = (
        "name", "brand", "model", "reference", "ean", "description",
        "category", "attributes", "color", "style", "material", "properties",
        "ProductSettings", "variants",
    )
    return _fold(" ".join(str(product.get(field) or "") for field in fields))


class ProductRetrievalCompiler:
    @staticmethod
    def compile(
        interpretation: SalesInterpretation,
        *,
        category_ids: tuple[str, ...] | list[str] = (),
    ) -> ProductRetrievalPlan:
        subject = interpretation.subject
        exact = bool(subject.reference or subject.ean or subject.model)
        requests: list[ProductRetrievalRequest] = []

        if subject.ean:
            requests.append(ProductRetrievalRequest(strategy="exact_ean", ean=subject.ean))
        elif subject.reference:
            requests.append(ProductRetrievalRequest(strategy="exact_reference", reference=subject.reference))
        elif subject.model:
            combined_name = " ".join(value for value in (subject.brand, subject.model) if value)
            requests.append(ProductRetrievalRequest(
                strategy="exact_model_with_brand" if subject.brand else "exact_model",
                name=subject.model,
                brand=subject.brand,
            ))
            if subject.brand and combined_name != subject.model:
                requests.append(ProductRetrievalRequest(
                    strategy="brand_candidates",
                    brand=subject.brand,
                ))
                requests.append(ProductRetrievalRequest(
                    strategy="exact_name_broad",
                    name=combined_name,
                ))
                requests.append(ProductRetrievalRequest(
                    strategy="exact_model_broad",
                    name=subject.model,
                ))
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

        return ProductRetrievalPlan(
            mode="exact" if exact else "recommendation",
            requests=tuple(requests),
        )


def effective_price(product: dict[str, Any]) -> float | None:
    for key in ("current_price", "promotional_price", "price"):
        value = product.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, str):
                normalized = value.replace("R$", "").strip()
                normalized = normalized.replace(".", "").replace(",", ".") if "," in normalized else normalized
                return float(normalized)
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


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
    values: list[bool] = []
    for key in ("available", "available_in_store", "available_for_purchase", "upon_request"):
        state = _truth_state(product.get(key))
        if state is not None:
            values.append(state)
    settings = product.get("ProductSettings")
    if isinstance(settings, dict):
        for key in ("available", "available_in_store", "available_for_purchase", "upon_request"):
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
    expected_reference = _fold(subject.reference)
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
        if mode == "exact" and expected_model:
            model_tokens = [token for token in expected_model.split() if token]
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
                "description": str(product.get("description") or "")[:240] or None,
                "properties": product.get("properties") or product.get("attributes"),
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
        return products[:CANDIDATE_POOL_LIMIT]
    compatible: list[dict[str, Any]] = []
    for product in products[:CANDIDATE_POOL_LIMIT]:
        candidate_brand = _fold(product.get("brand"))
        if candidate_brand and candidate_brand == expected_brand:
            compatible.append(product)
            continue
        if not candidate_brand and expected_brand in _product_text(product):
            compatible.append(product)
    return compatible


def exact_specific_product_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    subject = interpretation.subject
    candidates = _brand_compatible_candidates(products, interpretation)
    expected_reference = _fold(subject.reference)
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
    return matches


async def match_specific_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    compatible = _brand_compatible_candidates(products, interpretation)
    exact_matches = exact_specific_product_matches(compatible, interpretation)
    if exact_matches:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(exact_matches[:RERANK_SELECTION_LIMIT]),
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        return exact_matches[:RERANK_SELECTION_LIMIT]

    settings = get_settings()
    if not compatible or not settings.openai_api_key:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        return []

    candidate_by_id = {
        str(product["id"]): product
        for product in compatible
        if product.get("id") is not None
    }
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Identifique somente produtos reais que correspondam ao produto ou modelo "
                        "específico pedido. Use apenas os IDs de CANDIDATES. Uma marca igual, sem "
                        "relação suficiente com o modelo pedido, não é correspondência. Retorne lista "
                        "vazia quando nenhum candidato for semanticamente compatível. Não sugira "
                        "alternativas nesta seleção."
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
        )
        parsed = response.choices[0].message.parsed if response.choices else None
        if not isinstance(parsed, ProductMatchSelection):
            raise ValueError("product_match_schema_missing")
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
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "match_source": "openai",
        })
        return selected
    except (APIError, ValueError, TypeError) as exc:
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
            needs_evidence or bool(product.get("has_variation"))
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
        refreshed.append({**product, **result})
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
    settings = get_settings()
    fallback = _deterministic_semantic_order(products, interpretation)
    if not products or not settings.openai_api_key:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
        })
        return fallback

    candidate_by_id = {str(product["id"]): product for product in products if product.get("id") is not None}
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.parse(
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
                        "CANDIDATES": compact_candidates(products),
                    }, ensure_ascii=False),
                },
            ],
            response_format=ProductRerankSelection,
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
            "candidate_count": len(products),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
        })
        return selected
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "error_type": type(exc).__name__,
        })
        return fallback
