from __future__ import annotations

import os
import re
from typing import Any, Literal

from app.catalog.retrieval.limits import prefer_ready_stock_enabled
from app.catalog.retrieval.price import resolve_commercial_price
from app.catalog.retrieval.text import _fold

def apply_persona_presentation_order(
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stable reorder: ready-to-ship first when persona prefers urgency/stock."""
    if not products or not prefer_ready_stock_enabled():
        return products
    ready: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for product in products:
        if in_ready_to_ship_category(product):
            ready.append(product)
        else:
            other.append(product)
    if not ready:
        return products
    return ready + other

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


def ready_to_ship_category_ids() -> set[str]:
    raw = os.getenv("TRAY_READY_TO_SHIP_CATEGORY_IDS", "403")
    return {part.strip() for part in str(raw).replace(";", ",").split(",") if part.strip()}


def product_category_ids(product: dict[str, Any]) -> set[str]:
    found: list[str] = []

    def _collect(raw: Any) -> None:
        if raw in (None, ""):
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                _collect(item)
            return
        if isinstance(raw, dict):
            _collect(raw.get("id") or raw.get("category_id"))
            return
        found.append(str(raw).strip())

    _collect(product.get("category_id"))
    _collect(product.get("related_categories"))
    return {item for item in found if item}


def in_ready_to_ship_category(product: dict[str, Any]) -> bool:
    return bool(product_category_ids(product) & ready_to_ship_category_ids())


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
    ready_to_ship = in_ready_to_ship_category(product)
    if ready_to_ship:
        immediate_delivery_supported = True
        lead_time_days = 0
    stock = product.get("stock")
    return {
        "availability_state": product_availability_state(product),
        "has_stock": stock is not None,
        "stock": stock,
        "has_lead_time": lead_time_days is not None,
        "lead_time_days": lead_time_days,
        "immediate_delivery_supported": immediate_delivery_supported,
        "in_ready_to_ship_category": ready_to_ship,
    }
