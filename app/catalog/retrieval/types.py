from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app.catalog.retrieval.limits import (
    CANDIDATE_POOL_LIMIT,
    CATALOG_DISCOVERY_MAX_PAGES,
    CATALOG_DISCOVERY_MAX_PRODUCTS,
    CUSTOMER_RESULT_LIMIT,
    PRODUCT_PAGE_LIMIT,
)


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
    tokens: tuple[str, ...] = ()
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
                "tokens": list(self.tokens) if self.tokens else None,
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
