"""Invalidate product snapshot cache and durable catalog index from Tray events."""

from __future__ import annotations

from typing import Any

from app.catalog.index.catalog_index import to_canonical_item, upsert_canonical_items
from app.catalog.index.repository import CatalogIndexRepository
from app.catalog.index.snapshot import get_product_snapshot_cache
from app.config import get_settings
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError

_SNAPSHOT_KINDS = ["product", "stock", "price", "search", "image"]


def _persona_tenant() -> str:
    settings = get_settings()
    return str(getattr(settings, "agent_persona_tenant_id", None) or "newstore")


def _adapter_tenant(client: TrayAdapterClient) -> str:
    return str(client.base_url or "default")


def invalidate_snapshot(*, product_id: str, client: TrayAdapterClient | None = None) -> None:
    pid = str(product_id or "").strip()
    if not pid:
        return
    cache = get_product_snapshot_cache()
    tenants = {_persona_tenant()}
    if client is not None:
        tenants.add(_adapter_tenant(client))
    else:
        tenants.add(str(getattr(get_settings(), "tray_adapter_url", "") or "default"))
    for tenant_id in tenants:
        cache.invalidate(
            tenant_id=tenant_id,
            entity_id=pid,
            kinds=_SNAPSHOT_KINDS,
        )


def delete_index_product(product_id: str) -> int:
    pid = str(product_id or "").strip()
    if not pid:
        return 0
    return CatalogIndexRepository().delete_by_product_id(
        tenant_id=_persona_tenant(),
        product_id=pid,
    )


def delete_index_variant(variant_id: str) -> int:
    vid = str(variant_id or "").strip()
    if not vid:
        return 0
    return CatalogIndexRepository().delete_by_variant_id(
        tenant_id=_persona_tenant(),
        variant_id=vid,
    )


async def refresh_index_product(
    product_id: str,
    *,
    client: TrayAdapterClient | None = None,
) -> dict[str, Any]:
    pid = str(product_id or "").strip()
    if not pid:
        return {"ok": False, "reason": "missing_product_id"}
    tray = client or TrayAdapterClient()
    invalidate_snapshot(product_id=pid, client=tray)
    try:
        payload = await tray.get_product(pid)
    except TrayAdapterError as exc:
        deleted = delete_index_product(pid)
        return {
            "ok": False,
            "reason": "tray_get_failed",
            "status_code": exc.status_code,
            "deleted": deleted,
        }
    product = payload.get("product") if isinstance(payload, dict) else None
    if not isinstance(product, dict):
        product = payload if isinstance(payload, dict) else {}
    if payload.get("error") or not product.get("id"):
        deleted = delete_index_product(pid)
        return {"ok": False, "reason": "product_missing", "deleted": deleted}
    item = to_canonical_item(product, tenant_id=_persona_tenant(), factual_source="tray_search")
    written = upsert_canonical_items([item]) if item is not None else 0
    return {"ok": True, "product_id": pid, "upserted": written}


async def refresh_index_variant(
    variant_id: str,
    *,
    client: TrayAdapterClient | None = None,
) -> dict[str, Any]:
    vid = str(variant_id or "").strip()
    if not vid:
        return {"ok": False, "reason": "missing_variant_id"}
    tray = client or TrayAdapterClient()
    try:
        payload = await tray.get_product_variant(vid)
    except TrayAdapterError:
        deleted = delete_index_variant(vid)
        return {"ok": False, "reason": "tray_get_failed", "deleted": deleted}
    variant = payload.get("variant") if isinstance(payload, dict) else None
    if not isinstance(variant, dict):
        variant = payload if isinstance(payload, dict) else {}
    parent_id = str(variant.get("product_id") or "").strip()
    if parent_id:
        result = await refresh_index_product(parent_id, client=tray)
        result["variant_id"] = vid
        return result
    deleted = delete_index_variant(vid)
    return {"ok": True, "variant_id": vid, "deleted": deleted}
