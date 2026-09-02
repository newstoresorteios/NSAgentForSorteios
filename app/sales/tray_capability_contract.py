"""Pinned Tray Commerce list/search contract.

Source: https://developers.tray.com.br/ — API de Produtos / Listagem GET.
Reviewed against TRAYadaptor GET /internal/products (2026-09-01).

This is the documentation the query-authority agent consults. It is a snapshot
in process memory, not an HTTP fetch on each WhatsApp turn. Live scraping of
developers.tray.com.br per message would add hundreds of KB and seconds inside
the Vercel limit; refresh this module when the adaptor or Tray list params
change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet


TRAY_DOCS_URL = "https://developers.tray.com.br/"
TRAY_DOCS_SECTION = "API de Produtos / Listagem de Produtos GET"
TRAY_DOCS_REVIEWED_ON = "2026-09-02"

# GET https://{api_address}/products — query params from Tray docs.
TRAY_LIST_PRODUCTS_DOC_PARAMS: FrozenSet[str] = frozenset(
    {
        "access_token",
        "id",
        "name",
        "reference",
        "category_id",
        "ean",
        "price",
        "price_range",
        "brand",
        "available",
        "available_in_store",
        "stock",
        "promotion",
        "free_shipping",
        "release",
        "hot",
        "quantity_sold",
        "release_date",
        "rand",
        "sort",
        "limit",
        "page",
        "attrs",
        "created",
        "modified",
    }
)

# What TRAYadaptor GET /internal/products actually forwards today.
ADAPTER_SEARCH_PRODUCTS_PARAMS: FrozenSet[str] = frozenset(
    {
        "name",
        "reference",
        "ean",
        "brand",
        "category_id",
        "available",
        "available_in_store",
        "stock",
        "promotion",
        "limit",
        "page",
        "current_price_range",
        "property_name",
        "property_value",
        "model",
    }
)

# Documented on Tray, not forwarded by the adaptor as a max-price API.
# `price` is exact equality; `price_range` is a string field. Budget uses
# `current_price_range` plus local hard_filter.
ADAPTER_UNMAPPED_DOC_PARAMS: FrozenSet[str] = (
    TRAY_LIST_PRODUCTS_DOC_PARAMS
    - ADAPTER_SEARCH_PRODUCTS_PARAMS
    - {"access_token", "id", "rand", "sort", "attrs", "created", "modified"}
)

BUDGET_ENFORCEMENT = "tray_current_price_range_and_local_hard_filter"


@dataclass(frozen=True)
class TrayListProductsContract:
    docs_url: str
    docs_section: str
    reviewed_on: str
    tray_params: FrozenSet[str]
    adapter_params: FrozenSet[str]
    unmapped_price_params: FrozenSet[str]
    budget_enforcement: str
    notes: tuple[str, ...]


def consult_tray_list_products_contract() -> TrayListProductsContract:
    """Mandatory pre-search consult. In-process; no network."""
    unmapped_price = frozenset(
        name
        for name in ("price", "price_range")
        if name in TRAY_LIST_PRODUCTS_DOC_PARAMS
        and name not in ADAPTER_SEARCH_PRODUCTS_PARAMS
    )
    return TrayListProductsContract(
        docs_url=TRAY_DOCS_URL,
        docs_section=TRAY_DOCS_SECTION,
        reviewed_on=TRAY_DOCS_REVIEWED_ON,
        tray_params=TRAY_LIST_PRODUCTS_DOC_PARAMS,
        adapter_params=ADAPTER_SEARCH_PRODUCTS_PARAMS,
        unmapped_price_params=unmapped_price,
        budget_enforcement=BUDGET_ENFORCEMENT,
        notes=(
            "Tray GET /products accepts brand, name, reference, ean, category_id.",
            "Tray `price` is exact equality, not a ceiling.",
            "Tray `price_range` is a string filter; format is not a max_price API.",
            "TRAYadaptor forwards current_price_range, property_name/value, model.",
            "Tray `price` is exact equality, not a ceiling — do not send it as max_price.",
            "Tray `price_range` is a string filter; format is not a max_price API.",
            "Budget is sent as current_price_range and still hard-filtered locally.",
        ),
    )


def contract_as_log(contract: TrayListProductsContract | None = None) -> dict[str, Any]:
    item = contract or consult_tray_list_products_contract()
    return {
        "docs_url": item.docs_url,
        "docs_section": item.docs_section,
        "reviewed_on": item.reviewed_on,
        "adapter_params": sorted(item.adapter_params),
        "unmapped_price_params": sorted(item.unmapped_price_params),
        "budget_enforcement": item.budget_enforcement,
    }
