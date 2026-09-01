"""Post-hoc send gate: block product lists that violate scope or excluded brands."""

from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from ..catalog_specs import (
    excluded_brands_from_interpretation,
    product_matches_excluded_brand,
)
from ..commerce_context import CommerceConversationState
from ..models import AgentResult, SalesInterpretation

SCOPE_SEND_FALLBACK = (
    "Preciso confirmar no catálogo as opções que combinam com o que você pediu. "
    "Me dá um instante que já volto com sugestões certinhas."
)


class ScopeSendGateReport(BaseModel):
    valid: bool = True
    reason: str | None = None
    requested_brands: list[str] = Field(default_factory=list)
    excluded_brands: list[str] = Field(default_factory=list)
    presented_brands: list[str] = Field(default_factory=list)


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _presented_products(result: AgentResult) -> list[dict[str, Any]]:
    commercial = result.commercial_data or {}
    products = commercial.get("products") or []
    return [item for item in products if isinstance(item, dict)]


def _product_brand_label(product: dict[str, Any]) -> str:
    return str(product.get("brand") or "").strip()


def _brands_match(brand_a: str, brand_b: str) -> bool:
    folded_a = _fold(brand_a)
    folded_b = _fold(brand_b)
    if not folded_a or not folded_b:
        return False
    return folded_a == folded_b or folded_a in folded_b or folded_b in folded_a


def _product_matches_brand(product: dict[str, Any], brand: str) -> bool:
    if product_matches_excluded_brand(product, [brand]):
        return True
    label = _product_brand_label(product)
    return bool(label) and _brands_match(label, brand)


def requested_brands_from_context(
    interpretation: SalesInterpretation | None,
    message_text: str | None,
) -> list[str]:
    brands: list[str] = []
    if interpretation is not None and interpretation.subject.brand:
        brands.append(str(interpretation.subject.brand).strip())
    try:
        from .discovery import _mentioned_watch_brands

        brands.extend(_mentioned_watch_brands(message_text))
    except Exception:
        pass
    excluded = (
        excluded_brands_from_interpretation(interpretation)
        if interpretation is not None
        else []
    )
    excluded_fold = {_fold(item) for item in excluded}
    seen: set[str] = set()
    ordered: list[str] = []
    for brand in brands:
        key = _fold(brand)
        if not key or key in seen or key in excluded_fold:
            continue
        seen.add(key)
        ordered.append(brand)
    return ordered


def validate_scope_send_gate(
    result: AgentResult,
    *,
    interpretation: SalesInterpretation | None = None,
    message_text: str | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> ScopeSendGateReport:
    metadata = result.response_metadata or {}
    products = _presented_products(result)
    if not products:
        return ScopeSendGateReport(valid=True)
    if not metadata.get("presented_products"):
        return ScopeSendGateReport(valid=True)

    excluded = list(
        dict.fromkeys(
            excluded_brands_from_interpretation(interpretation)
            if interpretation is not None
            else []
        )
    )
    if commerce_state is not None and commerce_state.active_preferences:
        stored = commerce_state.active_preferences.get("excluded_brands")
        if isinstance(stored, list):
            for item in stored:
                label = str(item or "").strip()
                if label and label not in excluded:
                    excluded.append(label)

    requested = requested_brands_from_context(interpretation, message_text)
    presented_brands = [
        label for label in (_product_brand_label(item) for item in products) if label
    ]

    if excluded and all(
        product_matches_excluded_brand(product, excluded) for product in products
    ):
        return ScopeSendGateReport(
            valid=False,
            reason="all_excluded_brand",
            requested_brands=requested,
            excluded_brands=excluded,
            presented_brands=presented_brands,
        )

    if requested and not any(
        _product_matches_brand(product, brand)
        for product in products
        for brand in requested
    ):
        presented_fold = {_fold(brand) for brand in presented_brands}
        requested_fold = {_fold(brand) for brand in requested}
        if presented_fold and presented_fold.isdisjoint(requested_fold):
            return ScopeSendGateReport(
                valid=False,
                reason="off_scope_brand_list",
                requested_brands=requested,
                excluded_brands=excluded,
                presented_brands=presented_brands,
            )

    return ScopeSendGateReport(
        valid=True,
        requested_brands=requested,
        excluded_brands=excluded,
        presented_brands=presented_brands,
    )


def apply_scope_send_gate(
    result: AgentResult,
    *,
    interpretation: SalesInterpretation | None = None,
    message_text: str | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> tuple[AgentResult, ScopeSendGateReport]:
    report = validate_scope_send_gate(
        result,
        interpretation=interpretation,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    if report.valid:
        return result, report

    fixed = result.model_copy(deep=True)
    commercial = dict(fixed.commercial_data or {})
    commercial.pop("products", None)
    fixed.commercial_data = commercial or None
    fixed.reply_text = SCOPE_SEND_FALLBACK
    fixed.safety_reason = "scope_send_gate_blocked"
    metadata = dict(fixed.response_metadata or {})
    metadata["presented_products"] = False
    metadata["scope_send_gate"] = report.model_dump(mode="json")
    metadata["factual_fallback_text"] = SCOPE_SEND_FALLBACK
    fixed.response_metadata = metadata
    return fixed, report
