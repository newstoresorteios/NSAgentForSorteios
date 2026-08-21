"""Live Tray adaptor dependency probe for health / HA visibility."""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import get_settings
from .tray_circuit_breaker import circuit_status_dict


async def probe_tray_adaptor(*, timeout_s: float = 4.0) -> dict[str, Any]:
    """GET {TRAY_ADAPTER_URL}/health — no auth required on the adaptor."""
    settings = get_settings()
    base = str(getattr(settings, "tray_adapter_url", "") or "").rstrip("/")
    if not base:
        return {
            "configured": False,
            "ok": False,
            "reason": "tray_adapter_url_missing",
        }
    url = f"{base}/health"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(url)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        body: Any = None
        try:
            body = response.json()
        except Exception:
            body = (response.text or "")[:200]
        ok = 200 <= response.status_code < 300
        return {
            "configured": True,
            "ok": ok,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "url": url,
            "body": body if isinstance(body, dict) else {"raw": body},
            "circuit": circuit_status_dict(),
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "configured": True,
            "ok": False,
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "url": url,
            "error": type(exc).__name__,
            "circuit": circuit_status_dict(),
        }


def tray_ha_checklist(probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """Document what still needs Dashboard action for Tray HA."""
    probe = probe or {}
    return {
        "adaptor_health_reachable": bool(probe.get("ok")),
        "render_dashboard_url": (
            "https://dashboard.render.com/web/srv-d9fq41jtqb8s73dl4r80"
        ),
        "manual_steps_pending": [
            "Settings → Health Check Path = /health",
            "Scaling → Instance Count = 2 (starter ≈ +$7/mo)",
            "Optional: set TRAY_REFRESH_TOKEN env for OAuth bootstrap",
        ],
        "note": (
            "Blueprint already declares numInstances=2 + healthCheckPath=/health; "
            "live service may still show 1 instance until applied in Dashboard."
        ),
    }
