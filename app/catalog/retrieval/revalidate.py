from __future__ import annotations

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
        # Revalidation is factual authority: overlay live Tray fields but never
        # invent price/stock when the live payload omits them.
        current = {**product, **result}
        # Drop retrieval-only metadata from customer-facing payload later.
        current["commercial_availability"] = commercial_availability_facts(current)
        current["_revalidated"] = True
        current["_factual_source"] = "tray_live"
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
