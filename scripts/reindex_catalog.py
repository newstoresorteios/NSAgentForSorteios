"""Force-refresh ai_catalog_index from Tray + repair storefront URLs.

Usage (from repo root, with env loaded):
  python scripts/reindex_catalog.py

Loads secrets from .env.vercel.cron if present. Never prints secret values.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        if cleaned.upper() in {"[SENSITIVE]", "SENSITIVE"}:
            continue
        if key and key not in os.environ:
            os.environ[key] = cleaned


_load_dotenv(ROOT / ".env.vercel.cron")
_load_dotenv(ROOT / ".env.local")

# vercel env run may inject empty placeholders for Sensitive vars; drop them
# so pydantic Settings can use defaults instead of failing on "".
for _key, _value in list(os.environ.items()):
    if _value == "" or _value.strip().upper() in {"[SENSITIVE]", "SENSITIVE"}:
        os.environ.pop(_key, None)

from app.catalog.catalog_brand_warm import _DEFAULT_TOP_BRANDS  # noqa: E402
from app.catalog.catalog_index import index_products_best_effort  # noqa: E402
from app.catalog.catalog_url_health import repair_catalog_storefront_urls  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.tray.tray_tools import execute_tool  # noqa: E402


def _tenant() -> str:
    return str(getattr(get_settings(), "agent_persona_tenant_id", "") or "").strip() or "default"


def clear_brand_caches(brands: list[str]) -> int:
    cleared = 0
    keys = [f"brand:{' '.join(b.strip().lower().split())}" for b in brands if b.strip()]
    if not keys:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.ai_catalog_cache WHERE cache_key = ANY(%s)",
                (keys,),
            )
            cleared = int(cur.rowcount or 0)
        conn.commit()
    return cleared


async def fetch_brand_pages(
    brand: str,
    *,
    pages: int = 12,
    limit: int = 50,
    available: bool | None = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (products, exhausted). exhausted=True when Tray returned a short/empty page."""
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    exhausted = False
    for page in range(1, pages + 1):
        payload: dict[str, Any] = {
            "brand": brand,
            "limit": limit,
            "page": page,
        }
        if available is True:
            payload["available"] = True
            payload["available_in_store"] = True
        result = await execute_tool("search_products", payload)
        if "error" in result:
            print(
                "[reindex.brand.error]",
                {"brand": brand, "page": page, "error": result.get("error")},
            )
            return products, False
        batch = result.get("products") if isinstance(result.get("products"), list) else []
        if not batch:
            exhausted = True
            break
        for product in batch:
            if not isinstance(product, dict) or product.get("id") is None:
                continue
            pid = str(product["id"])
            if pid in seen:
                continue
            seen.add(pid)
            products.append(product)
        if len(batch) < limit:
            exhausted = True
            break
    return products, exhausted


def mark_missing_unavailable(brand: str, live_ids: set[str]) -> int:
    """Mark indexed rows for brand not seen in the live available pool as unavailable."""
    if not live_ids:
        return 0
    tenant = _tenant()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_catalog_index
                SET available = false,
                    stock = 0,
                    updated_at = now()
                WHERE tenant_id = %(tenant_id)s
                  AND brand ILIKE %(brand)s
                  AND product_id IS NOT NULL
                  AND NOT (product_id = ANY(%(live_ids)s))
                  AND COALESCE(available, true) IS TRUE
                """,
                {
                    "tenant_id": tenant,
                    "brand": brand,
                    "live_ids": list(live_ids),
                },
            )
            marked = int(cur.rowcount or 0)
        conn.commit()
    return marked


async def probe_and_clear_dead_urls(*, limit: int = 120) -> dict[str, int]:
    from app.catalog.product_media import ensure_product_has_live_url, official_product_url

    tenant = _tenant()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT catalog_item_key, url
                FROM public.ai_catalog_index
                WHERE tenant_id = %(tenant_id)s
                  AND url IS NOT NULL
                  AND btrim(url) <> ''
                ORDER BY freshness_at ASC NULLS FIRST
                LIMIT %(limit)s
                """,
                {"tenant_id": tenant, "limit": limit},
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]

    dead = 0
    live = 0
    cleared = 0
    for row in rows:
        key = str(row.get("catalog_item_key") or "").strip()
        url = str(row.get("url") or "").strip()
        if not key or not url:
            continue
        probed = await ensure_product_has_live_url({"url": url})
        kept = official_product_url(probed)
        if kept and not probed.get("_product_url_dead"):
            live += 1
            continue
        dead += 1
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.ai_catalog_index
                    SET url = NULL,
                        available = false,
                        updated_at = now()
                    WHERE tenant_id = %(tenant_id)s
                      AND catalog_item_key = %(key)s
                    """,
                    {"tenant_id": tenant, "key": key},
                )
                if cur.rowcount:
                    cleared += 1
            conn.commit()
    return {"probed": len(rows), "live": live, "dead": dead, "cleared": cleared}


async def main() -> None:
    settings = get_settings()
    if not (settings.database_url or os.getenv("DATABASE_URL")):
        raise SystemExit("DATABASE_URL missing")
    if not (settings.tray_adapter_url and settings.tray_adapter_token):
        raise SystemExit("TRAY_ADAPTER_URL/TOKEN missing")

    brands = list(_DEFAULT_TOP_BRANDS)
    print("[reindex.start]", {"brands": len(brands), "tenant": _tenant()})
    cleared_cache = clear_brand_caches(brands)
    print("[reindex.cache_cleared]", {"rows": cleared_cache})

    written = 0
    marked = 0
    warmed: list[str] = []
    for brand in brands:
        products, exhausted = await fetch_brand_pages(
            brand, pages=12, limit=50, available=True
        )
        if not products:
            print("[reindex.brand.empty]", {"brand": brand})
            continue
        n = index_products_best_effort(products, factual_source="tray_search")
        live_ids = {str(p["id"]) for p in products if p.get("id") is not None}
        # Only mark missing when we fully exhausted Tray pages (avoid false negatives).
        m = mark_missing_unavailable(brand, live_ids) if exhausted else 0
        written += n
        marked += m
        warmed.append(brand)
        print(
            "[reindex.brand.done]",
            {
                "brand": brand,
                "fetched": len(products),
                "written": n,
                "exhausted": exhausted,
                "marked_unavailable": m,
            },
        )

    repair = await repair_catalog_storefront_urls(limit=200, probe_live=True)
    print("[reindex.url_repair]", repair)
    dead = await probe_and_clear_dead_urls(limit=150)
    print("[reindex.dead_urls]", dead)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*) AS indexed,
                  count(*) FILTER (WHERE available IS TRUE) AS available_true,
                  count(*) FILTER (WHERE available IS FALSE) AS available_false,
                  count(*) FILTER (WHERE price IS NULL OR price::numeric <= 0) AS zero_price,
                  count(*) FILTER (WHERE freshness_at > now() - interval '1 day') AS fresh_1d,
                  max(freshness_at) AS freshest
                FROM public.ai_catalog_index
                WHERE tenant_id = %s
                """,
                (_tenant(),),
            )
            stats = dict(cur.fetchone() or {})
    print(
        "[reindex.done]",
        {
            "brands_warmed": warmed,
            "index_rows_written": written,
            "marked_unavailable": marked,
            "stats": {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in stats.items()
            },
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
