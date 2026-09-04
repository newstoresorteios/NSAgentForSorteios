"""IQ-06: catalog index as primary candidate source before Tray fan-out.

Order:
1. Exact identifiers (EAN / reference / SKU-like reference)
2. Constraint search (brand + budget + gender)
3. Lexical fallback (brand/model/query)

When the hard-filtered pool is large enough, sales retrieval skips blind Tray
probes and brand/category discovery. Tray still revalidates the top-N shown.
Insufficient / empty index falls through so brand/category refresh can refill
the durable index.
"""

from __future__ import annotations

from typing import Any

import app.catalog.retrieval.runtime as _runtime
from app.models import SalesInterpretation
from app.catalog.specs.preference_normalize import preference_gender_label
from app.catalog.product_retrieval import candidate_pool_limit, customer_result_limit


def primary_index_min_count(*, candidate_limit: int | None = None) -> int:
    """Minimum hard-filtered hits to treat the index as sufficient."""
    limit = int(candidate_limit or candidate_pool_limit() or 20)
    return min(limit, max(customer_result_limit() * 2, 5))


def _tenant_id() -> str:
    settings = _runtime.get_settings()
    return str(getattr(settings, "agent_persona_tenant_id", None) or "newstore")


def _query_text(interpretation: SalesInterpretation) -> str:
    subject = interpretation.subject
    return " ".join(
        part
        for part in (
            subject.brand,
            subject.model,
            getattr(subject, "query", None),
            subject.product_type,
        )
        if part
    ).strip()


def fetch_primary_index_candidates(
    interpretation: SalesInterpretation,
    *,
    limit: int | None = None,
    tenant_id: str | None = None,
    message_text: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return Tray-shaped products from ``ai_catalog_index`` and a strategy tag.

    Never raises — repository errors become an empty pool so Tray refresh runs.
    """
    settings = _runtime.get_settings()
    if not bool(getattr(settings, "agent_catalog_index_read_enabled", True)):
        return [], None

    from app.catalog.index.repository import CatalogIndexRepository, row_to_product_dict

    tenant = str(tenant_id or _tenant_id()).strip() or "newstore"
    lim = int(
        limit
        or getattr(settings, "agent_catalog_index_candidate_limit", 30)
        or 30
    )
    lim = max(1, min(lim, 100))
    repo = CatalogIndexRepository()
    subject = interpretation.subject
    prefs = interpretation.preferences
    rows: list[dict[str, Any]] = []
    strategy: str | None = None

    try:
        if subject.ean:
            rows = repo.search_exact(tenant_id=tenant, ean=str(subject.ean), limit=lim)
            strategy = "exact_ean" if rows else None
        elif subject.reference:
            rows = repo.search_exact(
                tenant_id=tenant,
                reference=str(subject.reference),
                limit=lim,
            )
            strategy = "exact_reference" if rows else None

        gender = preference_gender_label(interpretation)
        case_range = None
        try:
            from app.catalog.specs.catalog_specs import interpretation_case_size_range

            case_range = interpretation_case_size_range(
                interpretation,
                message_text=message_text,
            )
        except Exception:
            case_range = None
        min_case_mm, max_case_mm = case_range if case_range else (None, None)
        has_constraints = any(
            (
                subject.brand,
                prefs.budget_max is not None,
                gender,
                case_range,
            )
        )
        if not rows and has_constraints:
            rows = repo.search_by_constraints(
                tenant_id=tenant,
                brand=subject.brand,
                gender=gender,
                max_price=prefs.budget_max,
                min_case_size_mm=min_case_mm,
                max_case_size_mm=max_case_mm,
                limit=lim,
            )
            if rows:
                strategy = "constraints"

        if not rows:
            query = _query_text(interpretation)
            if query:
                rows = repo.search_lexical(
                    tenant_id=tenant,
                    query=query,
                    brand=subject.brand,
                    limit=lim,
                )
                if rows:
                    strategy = "lexical"
    except Exception as exc:  # noqa: BLE001
        print(
            "[catalog.index.primary.error]",
            {"error_type": type(exc).__name__},
        )
        return [], None

    products = [row_to_product_dict(row) for row in rows]
    return products, strategy


def index_pool_is_sufficient(
    hard_filtered: list[dict[str, Any]],
    *,
    candidate_limit: int | None = None,
) -> bool:
    return len(hard_filtered) >= primary_index_min_count(
        candidate_limit=candidate_limit
    )
