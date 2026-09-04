"""Repair and probe storefront URLs stored in ai_catalog_index."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.catalog.product_media import (
    ensure_product_has_live_url,
    normalize_storefront_brand_path,
    official_product_url,
)


def _tenant_id() -> str:
    settings = get_settings()
    return str(getattr(settings, "agent_persona_tenant_id", "") or "").strip() or "default"


def list_stale_brand_path_rows(*, limit: int = 100) -> list[dict[str, Any]]:
    tenant = _tenant_id()
    try:
        from app.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT catalog_item_key, product_id, reference, url, title_normalized
                    FROM public.ai_catalog_index
                    WHERE tenant_id = %(tenant_id)s
                      AND url ~ '/relogios-[a-z0-9-]+/'
                      AND url !~ '/relogios/relogios-'
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT %(limit)s
                    """,
                    {"tenant_id": tenant, "limit": max(1, min(int(limit), 500))},
                )
                return [dict(row) for row in (cur.fetchall() or [])]
    except Exception as exc:
        print("[catalog.url.list_stale.error]", {"error_type": type(exc).__name__})
        return []


def update_catalog_url(*, catalog_item_key: str, url: str | None) -> bool:
    tenant = _tenant_id()
    key = str(catalog_item_key or "").strip()
    if not tenant or not key:
        return False
    try:
        from app.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.ai_catalog_index
                    SET url = %(url)s,
                        updated_at = now()
                    WHERE tenant_id = %(tenant_id)s
                      AND catalog_item_key = %(catalog_item_key)s
                    """,
                    {
                        "tenant_id": tenant,
                        "catalog_item_key": key,
                        "url": url,
                    },
                )
                updated = bool(cur.rowcount)
            conn.commit()
        return updated
    except Exception as exc:
        print("[catalog.url.update.error]", {"error_type": type(exc).__name__})
        return False


async def repair_catalog_storefront_urls(
    *,
    limit: int = 40,
    probe_live: bool = True,
) -> dict[str, Any]:
    """Rewrite stale brand paths; optionally probe and clear soft-404 URLs."""
    rows = list_stale_brand_path_rows(limit=limit)
    rewritten = 0
    probed_live = 0
    probed_dead = 0
    unchanged = 0
    for row in rows:
        key = str(row.get("catalog_item_key") or "").strip()
        raw = str(row.get("url") or "").strip()
        if not key or not raw:
            continue
        normalized = normalize_storefront_brand_path(raw) or raw
        candidate = normalized
        if probe_live:
            probed = await ensure_product_has_live_url({"url": candidate})
            live = official_product_url(probed)
            if live and not probed.get("_product_url_dead"):
                candidate = live
                probed_live += 1
            else:
                # Keep rewritten path if probe fails — still better than stale slug.
                probed_dead += 1
        if candidate != raw:
            if update_catalog_url(catalog_item_key=key, url=candidate):
                rewritten += 1
            else:
                unchanged += 1
        else:
            unchanged += 1
    result = {
        "ok": True,
        "scanned": len(rows),
        "rewritten": rewritten,
        "probed_live": probed_live,
        "probed_dead": probed_dead,
        "unchanged": unchanged,
        "tenant_id": _tenant_id(),
    }
    print("[catalog.url.repair]", result)
    return result


def mark_stale_or_zero_price_unavailable(
    *,
    stale_days: int = 3,
    limit: int = 500,
) -> dict[str, Any]:
    """Fail-closed: available=true rows that are stale or price-less become unavailable.

    Does not delete rows — search/ranking should stop treating them as sellable.
    """
    tenant = _tenant_id()
    days = max(1, min(int(stale_days), 30))
    cap = max(1, min(int(limit), 2000))
    try:
        from app.db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH victims AS (
                        SELECT catalog_item_key
                        FROM public.ai_catalog_index
                        WHERE tenant_id = %(tenant_id)s
                          AND available IS TRUE
                          AND (
                            COALESCE(price, 0) <= 0
                            OR freshness_at IS NULL
                            OR freshness_at <= now() - make_interval(days => %(days)s)
                          )
                        ORDER BY freshness_at ASC NULLS FIRST
                        LIMIT %(limit)s
                    )
                    UPDATE public.ai_catalog_index AS idx
                    SET available = FALSE,
                        updated_at = now()
                    FROM victims
                    WHERE idx.tenant_id = %(tenant_id)s
                      AND idx.catalog_item_key = victims.catalog_item_key
                    RETURNING idx.catalog_item_key
                    """,
                    {"tenant_id": tenant, "days": days, "limit": cap},
                )
                marked = [dict(row) for row in (cur.fetchall() or [])]
            conn.commit()
        result = {
            "ok": True,
            "marked_unavailable": len(marked),
            "stale_days": days,
            "tenant_id": tenant,
        }
        print("[catalog.freshness.mark_unavailable]", result)
        return result
    except Exception as exc:
        print(
            "[catalog.freshness.mark_unavailable.error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:160]},
        )
        return {
            "ok": False,
            "marked_unavailable": 0,
            "error_type": type(exc).__name__,
            "tenant_id": tenant,
        }