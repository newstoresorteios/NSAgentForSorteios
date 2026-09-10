"""Vercel / Render cron routes — GET and POST share one handler per path."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request

from app.config import get_settings as _get_settings
from app.http.bindings import resolve
from app.learning.remarketing import run_remarketing_batch
from app.ops.observability import log_event
from app.security import verify_remarketing_cron

router = APIRouter(tags=["cron"])


def add_cron(path: str, endpoint: Callable) -> None:
    """Register GET+POST once. Vercel Hobby cron is GET and must still run the job."""
    router.add_api_route(
        path,
        endpoint,
        methods=["GET", "POST"],
        dependencies=[Depends(verify_remarketing_cron)],
    )


async def catalog_url_health_cron(
    request: Request,
    brand_limit: int = Query(default=8, ge=1, le=25),
    products_per_brand: int = Query(default=40, ge=5, le=200),
    url_limit: int = Query(default=50, ge=1, le=500),
    clear_brand_cache: bool = Query(default=False),
):
    from app.catalog.index.warm import (
        _DEFAULT_TOP_BRANDS,
        list_top_index_brands,
        refresh_top_brands_into_index,
    )
    from app.catalog.media.url_health import (
        mark_stale_or_zero_price_unavailable,
        repair_catalog_storefront_urls,
    )
    from app.tray.tray_tools import execute_tool

    # GET (Vercel cron) must not honor extra mutating query flags.
    allow_clear = request.method.upper() == "POST" and bool(clear_brand_cache)
    if allow_clear:
        try:
            from app.db import get_conn

            brands = list_top_index_brands(limit=brand_limit) or list(
                _DEFAULT_TOP_BRANDS[:brand_limit]
            )
            keys = [
                f"brand:{' '.join(str(b).strip().lower().split())}"
                for b in brands
                if str(b).strip()
            ]
            if keys:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM public.ai_catalog_cache WHERE cache_key = ANY(%s)",
                            (keys,),
                        )
                    conn.commit()
        except Exception as exc:  # noqa: BLE001
            log_event(
                "catalog.url_health.cache_clear_error",
                {"error_type": type(exc).__name__},
            )

    result = await repair_catalog_storefront_urls(limit=url_limit, probe_live=True)
    freshness = mark_stale_or_zero_price_unavailable(stale_days=3, limit=500)
    warm = await refresh_top_brands_into_index(
        execute_tool,
        brand_limit=brand_limit,
        products_per_brand=products_per_brand,
    )
    payload = {**result, "freshness": freshness, "brand_warm": warm}
    log_event("catalog.url_health.cron.completed", payload)
    return payload


async def tray_keepalive_cron():
    """Keep TrayAdaptor warm and refresh OAuth when access looks unhealthy."""
    import httpx
    from urllib.parse import urlparse

    settings = resolve("get_settings", _get_settings)()
    base = (settings.tray_adapter_url or "").rstrip("/")
    if not base:
        return {"ok": False, "skipped": True, "reason": "tray_adapter_url_missing"}
    started = __import__("time").monotonic()
    payload: dict[str, Any] = {
        "ok": False,
        "url_host": urlparse(base).netloc,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            health = await client.get(f"{base}/health")
            payload["health_status_code"] = health.status_code
            try:
                health_body = health.json()
                if isinstance(health_body, dict):
                    payload["tray_status"] = health_body.get("status")
                    payload["tray_build"] = health_body.get("build")
            except ValueError:
                pass

            tray_health = await client.get(f"{base}/health/tray")
            payload["tray_health_status_code"] = tray_health.status_code
            access_valid = False
            try:
                tray_body = tray_health.json()
                if isinstance(tray_body, dict):
                    access_valid = bool(tray_body.get("access_valid"))
                    payload["access_valid"] = access_valid
                    payload["bootstrap_refresh_configured"] = bool(
                        tray_body.get("bootstrap_refresh_configured")
                    )
                    payload["database_cache_configured"] = bool(
                        tray_body.get("database_cache_configured")
                    )
                    payload["access_expires_at"] = tray_body.get("access_expires_at")
            except ValueError:
                pass

            payload["elapsed_ms"] = int((__import__("time").monotonic() - started) * 1000)
            payload["ok"] = (
                health.status_code == 200 and tray_health.status_code == 200
            )
            try:
                from app.tray.tray_sync import run_tray_sync

                payload["sync"] = await run_tray_sync()
            except Exception as exc:  # noqa: BLE001
                payload["sync_error"] = type(exc).__name__
            log_event("tray.keepalive", payload)
            return payload
    except Exception as exc:  # noqa: BLE001
        payload["elapsed_ms"] = int((__import__("time").monotonic() - started) * 1000)
        payload["error"] = type(exc).__name__
        log_event("tray.keepalive", payload)
        return payload


async def tray_sync_cron():
    from app.tray.tray_sync import run_tray_sync

    result = await run_tray_sync()
    log_event("tray.sync.cron.completed", result if isinstance(result, dict) else {})
    return result


async def remarketing_cron():
    result = await run_remarketing_batch()
    log_event(
        "remarketing.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return {"ok": True, **result}


async def product_image_index_cron():
    from app.catalog.vision.product_image_index import run_product_image_index_batch

    result = await run_product_image_index_batch()
    log_event(
        "product_image_index.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


async def attendance_learning_cron():
    from app.learning.attendance_learning import run_attendance_learning_batch

    result = await run_attendance_learning_batch()
    log_event(
        "attendance.learning.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


async def order_tracking_audit_cron():
    from app.commerce.order_tracking_audit import run_order_tracking_audit_batch
    from app.tray.tray_tools import execute_tool

    result = await run_order_tracking_audit_batch(execute=execute_tool)
    log_event("order.tracking_audit.cron.completed", result)
    return result


async def cron_instagram_story_media_retention():
    from app.stories.story_media_retention import cleanup_expired_story_media

    result = await cleanup_expired_story_media(limit=200)
    return {
        "ok": True,
        "scanned": result.scanned,
        "deleted_storage": result.deleted_storage,
        "cleared_paths": result.cleared_paths,
        "failed": result.failed,
    }


async def cron_process_inbox():
    """Safety-net drain. Vercel Hobby is daily; Render can poll more often."""
    from app.ingress.worker import process_inbox_batch

    return await process_inbox_batch()


async def cron_process_outbox():
    """Retry accepted outbound rows. Vercel Hobby is daily."""
    from app.ingress.outbox_worker import process_outbox_batch

    return await process_outbox_batch()


add_cron("/api/cron/catalog-url-health", catalog_url_health_cron)
add_cron("/api/cron/tray-keepalive", tray_keepalive_cron)
add_cron("/api/cron/tray-sync", tray_sync_cron)
add_cron("/api/cron/remarketing", remarketing_cron)
add_cron("/api/cron/product-image-index", product_image_index_cron)
add_cron("/api/cron/attendance-learning", attendance_learning_cron)
add_cron("/api/cron/order-tracking-audit", order_tracking_audit_cron)
add_cron("/api/cron/instagram-story-media-retention", cron_instagram_story_media_retention)
add_cron("/api/cron/process-inbox", cron_process_inbox)
add_cron("/api/cron/process-outbox", cron_process_outbox)
