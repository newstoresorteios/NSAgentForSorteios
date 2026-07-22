from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings
from .models import SalesInterpretation


CANDIDATE_POOL_LIMIT = 20
CUSTOMER_RESULT_LIMIT = 3
RERANK_SELECTION_LIMIT = 5


class ProductRerankSelection(BaseModel):
    selected_product_ids: list[str]


@dataclass(frozen=True)
class ProductRetrievalRequest:
    strategy: str
    name: str | None = None
    brand: str | None = None
    reference: str | None = None
    ean: str | None = None
    available: bool | None = None
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
                "available": self.available,
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
    )
    return _fold(" ".join(str(product.get(field) or "") for field in fields))


class ProductRetrievalCompiler:
    @staticmethod
    def compile(interpretation: SalesInterpretation) -> ProductRetrievalPlan:
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
                strategy="exact_brand_model" if subject.brand else "exact_model",
                name=combined_name,
                brand=subject.brand,
            ))
            if subject.brand and combined_name != subject.model:
                requests.append(ProductRetrievalRequest(
                    strategy="exact_model_with_brand",
                    name=subject.model,
                    brand=subject.brand,
                ))
                requests.append(ProductRetrievalRequest(
                    strategy="exact_model_broad",
                    name=subject.model,
                ))
        else:
            available = True
            if subject.product_type:
                requests.append(ProductRetrievalRequest(
                    strategy="product_type_with_brand" if subject.brand else "product_type",
                    name=subject.product_type,
                    brand=subject.brand,
                    available=available,
                ))
                if subject.brand:
                    requests.append(ProductRetrievalRequest(
                        strategy="product_type_broad",
                        name=subject.product_type,
                        available=available,
                    ))
            elif subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="explicit_brand",
                    brand=subject.brand,
                    available=available,
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
            }.items()
            if value is not None
        })
    return compact


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
