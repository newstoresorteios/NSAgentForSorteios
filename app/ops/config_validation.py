"""Cross-setting diagnostics for deployment profiles.

These checks report unsafe or contradictory combinations without exposing
secret values.  They deliberately do not abort startup: operators can inspect
them through the authenticated admin health endpoint before changing rollout.
"""

from __future__ import annotations

from typing import Any


def configuration_warnings(settings: Any) -> list[str]:
    warnings: list[str] = []

    if bool(getattr(settings, "agent_async_ingress_enabled", False)) and not str(
        getattr(settings, "database_url", "") or ""
    ).strip():
        warnings.append("async_ingress_requires_database")

    if bool(getattr(settings, "agent_memory_auto_apply_enabled", False)) and not str(
        getattr(settings, "agent_memory_auto_apply_sender_allowlist", "") or ""
    ).strip():
        warnings.append("memory_auto_apply_requires_sender_allowlist")

    tray_url = bool(str(getattr(settings, "tray_adapter_url", "") or "").strip())
    tray_token = bool(str(getattr(settings, "tray_adapter_token", "") or "").strip())
    if tray_url != tray_token:
        warnings.append("tray_adapter_url_and_token_must_be_configured_together")

    if bool(getattr(settings, "pix_direct_enabled", False)):
        resolved = getattr(settings, "resolved_mp_access_token", None)
        mp_token = resolved() if callable(resolved) else (
            getattr(settings, "mp_access_token", "")
            or getattr(settings, "mercadopago_access_token", "")
        )
        if not str(mp_token or "").strip():
            warnings.append("pix_direct_requires_mercadopago_token")
        if not str(getattr(settings, "public_url", "") or "").strip():
            warnings.append("pix_direct_requires_public_url")

    if (
        bool(getattr(settings, "agent_critique_enforce_on_commerce", False))
        and str(getattr(settings, "agent_critique_mode", "off") or "off").casefold()
        != "enforce"
    ):
        warnings.append("commerce_critique_flag_requires_enforce_mode")

    if (
        str(getattr(settings, "environment", "") or "").casefold() == "production"
        and not bool(getattr(settings, "dry_run", True))
        and not str(getattr(settings, "brevo_webhook_secret", "") or "").strip()
    ):
        warnings.append("live_production_requires_brevo_webhook_secret")

    return warnings
