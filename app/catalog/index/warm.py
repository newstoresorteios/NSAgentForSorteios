"""Warm top brands into durable catalog index for exact/near-match recovery."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.catalog.index.cache import fetch_and_cache_brand_pool
from app.catalog.index.catalog_index import index_products_best_effort
from app.config import get_settings

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

def configured_warm_brands() -> list[str]:
    from app.configuration.runtime import policy
    return list(dict.fromkeys(line.strip() for line in str(policy("catalogWarmBrands")).splitlines() if line.strip()))



def list_top_index_brands(*, limit: int = 10) -> list[str]:
    settings = get_settings()
    tenant = str(getattr(settings, "agent_persona_tenant_id", "") or "").strip() or "default"
    try:
        from app.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand, COUNT(*)::int AS n
                    FROM public.ai_catalog_index
                    WHERE tenant_id = %(tenant_id)s
                      AND brand IS NOT NULL
                      AND btrim(brand) <> ''
                    GROUP BY brand
                    ORDER BY max(freshness_at) ASC NULLS FIRST, n ASC, brand ASC
                    LIMIT %(limit)s
                    """,
                    {"tenant_id": tenant, "limit": max(1, min(int(limit), 25))},
                )
                rows = cur.fetchall() or []
        brands: list[str] = []
        for row in rows:
            brand = row.get("brand") if isinstance(row, dict) else row[0]
            text = str(brand or "").strip()
            if text and text not in brands:
                brands.append(text)
        return brands
    except Exception as exc:
        print("[catalog.top_brands.error]", {"error_type": type(exc).__name__})
        return []


async def refresh_top_brands_into_index(
    execute_tool: ToolExecutor,
    *,
    brand_limit: int = 8,
    products_per_brand: int = 80,
) -> dict[str, Any]:
    """Fetch Tray brand pools and write-through to ai_catalog_index."""
    known = list_top_index_brands(limit=25)
    configured = configured_warm_brands()
    missing = [brand for brand in configured if brand.casefold() not in {x.casefold() for x in known}]
    brands = (missing + known + configured)[:brand_limit]
    written = 0
    warmed: list[str] = []
    for brand in brands:
        pool = await fetch_and_cache_brand_pool(brand, execute_tool, pages=max(1, (products_per_brand + 49) // 50), limit=50,
                                              force_refresh=True, include_unavailable=True)
        if not pool:
            continue
        slice_products = pool[: max(5, min(int(products_per_brand), 200))]
        written += index_products_best_effort(
            slice_products,
            factual_source="tray_search",
        )
        warmed.append(brand)
    return {
        "brands_requested": brands,
        "brands_warmed": warmed,
        "index_rows_written": written,
    }
