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

_RETRYABLE_SCOPE_GATE_REASONS = frozenset({
    "all_excluded_brand",
    "off_scope_brand_list",
})

_EXCLUDE_BRAND_ATTR_PREFIX = "exclude_brand:"


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
    message_brands: list[str] = []
    try:
        from .discovery import _mentioned_watch_brands

        message_brands = _mentioned_watch_brands(message_text)
    except Exception:
        message_brands = []

    brands: list[str] = []
    sticky = ""
    if interpretation is not None and interpretation.subject.brand:
        sticky = str(interpretation.subject.brand).strip()
    include_sticky = bool(sticky) and (
        not message_brands
        or any(_brands_match(sticky, brand) for brand in message_brands)
    )
    if include_sticky:
        brands.append(sticky)
    brands.extend(message_brands)
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


def _append_exclude_brand(attrs: list[str], brand: str) -> None:
    label = f"{_EXCLUDE_BRAND_ATTR_PREFIX}{brand}"
    if label not in attrs:
        attrs.append(label)


def build_scope_corrected_interpretation(
    interpretation: SalesInterpretation,
    report: ScopeSendGateReport,
    *,
    message_text: str | None = None,
) -> SalesInterpretation:
    """Build a retrieval-ready interpretation after a scope send-gate block."""
    corrected = interpretation.model_copy(deep=True)
    attrs = list(corrected.preferences.attributes or [])
    excluded = list(report.excluded_brands or [])
    presented = list(report.presented_brands or [])
    requested = list(report.requested_brands or [])

    if report.reason == "all_excluded_brand":
        for brand in excluded or presented:
            _append_exclude_brand(attrs, brand)
        sticky = str(corrected.subject.brand or "").strip()
        if sticky and _fold(sticky) in {_fold(brand) for brand in (excluded or presented)}:
            corrected.subject.brand = None

    elif report.reason == "off_scope_brand_list":
        presented_fold = {_fold(brand) for brand in presented}
        sticky = str(corrected.subject.brand or "").strip()
        if sticky and _fold(sticky) in presented_fold:
            corrected.subject.brand = None
        for brand in presented:
            if brand:
                _append_exclude_brand(attrs, brand)
        message_brands: list[str] = []
        try:
            from .discovery import _mentioned_watch_brands

            message_brands = _mentioned_watch_brands(message_text)
        except Exception:
            message_brands = []
        if len(message_brands) == 1:
            corrected.subject.brand = message_brands[0]
        elif (
            not corrected.subject.brand
            and len(requested) == 1
            and _fold(requested[0]) not in presented_fold
        ):
            corrected.subject.brand = requested[0]

    corrected.preferences.attributes = attrs
    corrected.ready_for_retrieval = True
    corrected.enough_information_to_search = True
    corrected.stop_clarification = True
    corrected.needs_clarification = False
    return corrected


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


async def apply_scope_send_gate_with_retry(
    result: AgentResult,
    *,
    interpretation: SalesInterpretation | None = None,
    message_text: str | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> tuple[AgentResult, ScopeSendGateReport, SalesInterpretation | None]:
    """Validate scope gate; on block, re-retrieve once with corrected filters."""
    report = validate_scope_send_gate(
        result,
        interpretation=interpretation,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    if report.valid:
        return result, report, interpretation

    blocked, blocked_report = apply_scope_send_gate(
        result,
        interpretation=interpretation,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    if (
        interpretation is None
        or blocked_report.reason not in _RETRYABLE_SCOPE_GATE_REASONS
    ):
        return blocked, blocked_report, interpretation

    corrected = build_scope_corrected_interpretation(
        interpretation,
        blocked_report,
        message_text=message_text,
    )
    try:
        from .product_lookup import execute_compiled_product_retrieval

        retry_result = await execute_compiled_product_retrieval(corrected)
    except Exception:
        return blocked, blocked_report, interpretation

    if retry_result is None:
        return blocked, blocked_report, interpretation

    retry_report = validate_scope_send_gate(
        retry_result,
        interpretation=corrected,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    if not retry_report.valid:
        return blocked, blocked_report, None

    metadata = dict(retry_result.response_metadata or {})
    metadata["scope_send_gate_retry"] = {
        "original_reason": blocked_report.reason,
        "corrected": True,
    }
    if isinstance(metadata.get("interpretation"), dict):
        metadata["interpretation"] = corrected.model_dump(mode="json")
    retry_result.response_metadata = metadata
    return retry_result, retry_report, corrected
