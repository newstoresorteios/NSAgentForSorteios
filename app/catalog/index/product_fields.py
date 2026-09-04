"""Shared Tray/search dict field mapping for cache, snapshot, and canonical items."""

from __future__ import annotations

from typing import Any

from app.catalog.retrieval.price import effective_price


def product_id_of(product: dict[str, Any]) -> str | None:
    raw = product.get("id") or product.get("product_id") or product.get("ProductID")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def listed_promotional_price(product: dict[str, Any]) -> Any:
    return product.get("promotional_price") if product.get("promotional_price") is not None else product.get("sale_price")


def compact_catalog_fields(product: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal durable cache row. ``price`` is the commercial amount (current → promo → list)."""
    product_id = product_id_of(product)
    if not isinstance(product, dict) or product_id is None:
        return None
    return {
        key: value
        for key, value in {
            "id": product_id,
            "name": product.get("name"),
            "brand": product.get("brand"),
            "model": product.get("model"),
            "reference": product.get("reference"),
            "color": product.get("color"),
            "category": product.get("category") or product.get("category_name"),
            "description": str(product.get("description") or "")[:240] or None,
            "price": effective_price(product),
            "stock": product.get("stock"),
            "available": product.get("available"),
            "available_in_store": product.get("available_in_store"),
            "url": product.get("url"),
            "primary_image_url": product.get("primary_image_url")
            or product.get("image_url"),
        }.items()
        if value is not None
    }


def snapshot_list_price(product: dict[str, Any]) -> Any:
    """Raw list/current amount; promotional is stored separately on the snapshot."""
    for key in ("current_price", "price", "Price"):
        if product.get(key) is not None:
            return product.get(key)
    return listed_promotional_price(product)
