from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import SalesInterpretation
from app.catalog.retrieval.availability import commercial_availability_facts
from app.catalog.retrieval.limits import ToolExecutor, revalidate_top_n

async def revalidate_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    execute_tool: ToolExecutor,
) -> tuple[list[dict[str, Any]], bool]:
    refreshed: list[dict[str, Any]] = []
    failed = False
    partial = False
    top_n = revalidate_top_n()
    attempted = 0
    for product in products[:top_n]:
        product_id = product.get("id")
        if product_id is None:
            continue
        attempted += 1
        result = await execute_tool("get_product", {"product_id": str(product_id)})
        if "error" in result:
            failed = True
            partial = True
            status_code = result.get("status_code")
            # Upstream unhealthy / rate-limited: stop hammering remaining SKUs.
            if status_code in (429, 503, 502, 504):
                print(
                    "[sales.revalidate.abort]",
                    {
                        "status_code": status_code,
                        "attempted": attempted,
                        "confirmed": len(refreshed),
                    },
                )
                break
            continue
        # Overlay only live-present fields. Omitted price/stock must not inherit cache.
        live_commercial = frozenset(
            {
                "price",
                "current_price",
                "promotional_price",
                "stock",
                "available",
                "url",
            }
        )
        current = dict(product)
        live_confirmed: set[str] = set()
        for key, value in result.items():
            if str(key).startswith("_"):
                continue
            if value not in (None, ""):
                current[key] = value
                live_confirmed.add(str(key))
        for key in live_commercial:
            if key in current and key not in live_confirmed:
                current.pop(key, None)
        now = datetime.now(timezone.utc).isoformat()
        current["_field_sources"] = {
            key: "tray_live" for key in live_confirmed & live_commercial
        }
        current["_freshness_at"] = now
        current["_revalidated"] = bool(live_confirmed & live_commercial)
        current["_factual_source"] = (
            "tray_live"
            if current["_revalidated"]
            else str(product.get("_factual_source") or "catalog_index")
        )
        current["commercial_availability"] = commercial_availability_facts(current)
        print("[sales.availability.fact]", {
            "has_stock": current["commercial_availability"]["has_stock"],
            "has_lead_time": current["commercial_availability"]["has_lead_time"],
            "immediate_delivery_supported": current["commercial_availability"]["immediate_delivery_supported"],
            "revalidated": True,
        })
        refreshed.append(current)
    if attempted and not refreshed:
        failed = True
        print("[sales.revalidate.total_failure]", {"attempted": attempted})
    elif partial and refreshed:
        print(
            "[sales.revalidate.partial]",
            {
                "attempted": attempted,
                "confirmed": len(refreshed),
                "dropped_stale": attempted - len(refreshed),
            },
        )
    if refreshed:
        from app.catalog.retrieval.variants import enrich_product_variants

        refreshed = await enrich_product_variants(refreshed, interpretation, execute_tool)
    # Never present non-revalidated siblings when revalidation partially failed —
    # only confirmed Tray rows may assert live price/stock.
    return refreshed, failed
