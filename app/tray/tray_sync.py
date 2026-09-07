"""Combined Tray webhook consume + PIX settlement retry for crons."""

from __future__ import annotations

from typing import Any

from app.commerce.pix_settlement import retry_failed_pix_settlements
from app.tray.tray_webhook_consumer import consume_tray_webhook_events


async def run_tray_sync(*, event_limit: int = 50, pix_limit: int = 10) -> dict[str, Any]:
    webhooks = await consume_tray_webhook_events(limit=event_limit)
    pix = await retry_failed_pix_settlements(limit=pix_limit)
    return {
        "ok": bool(webhooks.get("ok")) and bool(pix.get("ok")),
        "webhooks": webhooks,
        "pix": pix,
    }
