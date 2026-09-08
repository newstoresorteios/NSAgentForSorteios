"""Public liveness vs admin diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config import (
    audio_outbound_ready,
    get_allowed_channels,
    get_settings,
    supabase_storage_configured,
)
from app.http.bindings import resolve
from app.http.version import AGENT_VERSION
from app.ops.rollout import build_rollout_status
from app.security import verify_admin_token
from app.tray.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.tray.tray_circuit_breaker import circuit_status_dict
from app.tray.tray_health_probe import probe_tray_adaptor, tray_ha_checklist
from app.channels.brevo_client import resolved_brevo_whatsapp_send_url

router = APIRouter(tags=["health"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-health"])


def public_health_payload(settings=None) -> dict:
    cfg = settings or resolve("get_settings", get_settings)()
    return {
        "ok": True,
        "service": getattr(cfg, "app_name", "NewStoreAgent"),
        "agent_version": AGENT_VERSION,
        "dry_run": bool(getattr(cfg, "dry_run", False)),
        "environment": getattr(cfg, "environment", ""),
        "database_configured": bool(getattr(cfg, "database_url", "")),
    }


async def admin_diagnostics_payload(settings=None) -> dict:
    cfg = settings or resolve("get_settings", get_settings)()
    openai_key = cfg.openai_api_key
    allowed_channels = get_allowed_channels(cfg)
    ordered_channels = [
        channel
        for channel in ("whatsapp", "instagram", "facebook")
        if channel in allowed_channels
    ]
    ordered_channels.extend(sorted(allowed_channels.difference(ordered_channels)))
    tray_probe = await probe_tray_adaptor()
    brevo_send_resolved, brevo_send_rewrite = resolved_brevo_whatsapp_send_url(
        cfg.brevo_send_url
    )
    from app.channels.meta_instagram import probe_instagram_graph_subscriptions

    try:
        meta_graph = await probe_instagram_graph_subscriptions()
    except Exception as exc:  # noqa: BLE001
        meta_graph = {"ok": False, "error": type(exc).__name__}

    return {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "agent_mode": "openai_with_db_context",
        "openai_configured": bool(openai_key),
        "openai_key_format_ok": openai_key.startswith(("sk-", "sk-proj-")),
        "openai_key_length": len(openai_key),
        "openai_model": cfg.openai_model,
        "agent_runtime_enabled": getattr(cfg, "agent_runtime_enabled", True),
        "agent_full_obs_logs": getattr(cfg, "agent_full_obs_logs", False),
        "agent_http_obs_logs": getattr(cfg, "agent_http_obs_logs", False),
        "agent_llm_budget_enabled": getattr(cfg, "agent_llm_budget_enabled", False),
        "agent_max_llm_calls_per_turn": getattr(cfg, "agent_max_llm_calls_per_turn", 2),
        "agent_policy_mode": getattr(cfg, "agent_policy_mode", "shadow"),
        "agent_factual_validation_mode": getattr(
            cfg, "agent_factual_validation_mode", "shadow"
        ),
        "agent_conversation_lock_enabled": getattr(
            cfg, "agent_conversation_lock_enabled", True
        ),
        "agent_async_ingress_enabled": getattr(
            cfg, "agent_async_ingress_enabled", False
        ),
        "agent_quality_judge_mode": getattr(cfg, "agent_quality_judge_mode", "shadow"),
        "agent_answer_council_enabled": getattr(
            cfg, "agent_answer_council_enabled", True
        ),
        "agent_answer_council_max_restarts": getattr(
            cfg, "agent_answer_council_max_restarts", 1
        ),
        "agent_critique_mode": getattr(cfg, "agent_critique_mode", "enforce"),
        "agent_critique_enforce_on_commerce": getattr(
            cfg, "agent_critique_enforce_on_commerce", True
        ),
        "agent_critique_max_retries": getattr(cfg, "agent_critique_max_retries", 2),
        "agent_history_limit": getattr(cfg, "agent_history_limit", 12),
        "agent_history_hard_cap": getattr(cfg, "agent_history_hard_cap", 80),
        "agent_max_recent_turns": getattr(cfg, "agent_max_recent_turns", 12),
        "agent_send_idempotency_enabled": getattr(
            cfg, "agent_send_idempotency_enabled", True
        ),
        "database_configured": bool(cfg.database_url),
        "sorteio_database_configured": bool(
            getattr(cfg, "sorteio_database_url", "") or cfg.database_url
        ),
        "sorteio_database_dedicated": bool(
            str(getattr(cfg, "sorteio_database_url", "") or "").strip()
        ),
        "brevo_send_configured": bool(
            cfg.brevo_api_key
            and (
                cfg.brevo_agent_id
                or (cfg.brevo_agent_email and cfg.brevo_agent_name)
                or cfg.brevo_sender_number
            )
        ),
        "brevo_conversations_configured": bool(
            cfg.brevo_api_key
            and (cfg.brevo_agent_id or (cfg.brevo_agent_email and cfg.brevo_agent_name))
        ),
        "brevo_whatsapp_configured": bool(cfg.brevo_api_key and cfg.brevo_sender_number),
        "brevo_social_channels_enabled": getattr(
            cfg, "brevo_social_channels_enabled", True
        ),
        "brevo_allowed_channels": ordered_channels,
        "brevo_reply_mode": cfg.brevo_reply_mode,
        "brevo_live_send_enabled": (
            not cfg.dry_run and cfg.brevo_reply_mode.lower() != "dry_run"
        ),
        "brevo_webhook_secret_configured": bool(cfg.brevo_webhook_secret),
        "brevo_whatsapp_send_url": {
            "uses_sendmessage_path": "/whatsapp/sendMessage" in brevo_send_resolved,
            "rewritten_from_env": bool(brevo_send_rewrite),
            "rewrite_reason": brevo_send_rewrite,
        },
        "audio_inbound_enabled": cfg.audio_inbound_enabled,
        "audio_outbound_enabled": cfg.audio_outbound_enabled,
        "audio_outbound_ready": audio_outbound_ready(cfg),
        "supabase_storage_configured": supabase_storage_configured(cfg),
        "dry_run": cfg.dry_run,
        "tray_adapter_configured": bool(cfg.tray_adapter_url and cfg.tray_adapter_token),
        "tray_circuit_breaker": circuit_status_dict(),
        "tray_adaptor_probe": tray_probe,
        "tray_ha": tray_ha_checklist(tray_probe),
        "tray_tools_enabled": bool(cfg.tray_adapter_url and cfg.tray_adapter_token),
        "pix_direct_enabled": bool(getattr(cfg, "pix_direct_enabled", False)),
        "pix_mp_configured": bool(
            (
                getattr(cfg, "resolved_mp_access_token", None)()
                if callable(getattr(cfg, "resolved_mp_access_token", None))
                else (
                    getattr(cfg, "mp_access_token", "")
                    or getattr(cfg, "mercadopago_access_token", "")
                )
            )
        ),
        "pix_public_url_configured": bool(getattr(cfg, "public_url", "")),
        "pix_webhook_path": "/api/payments/webhook",
        "remarketing_enabled": getattr(cfg, "remarketing_enabled", False),
        "remarketing_cron_configured": bool(
            getattr(cfg, "remarketing_cron_secret", "")
        ),
        "remarketing_touch_hours": getattr(cfg, "remarketing_touch_hours", "1,12,23"),
        "remarketing_meta_window_hours": getattr(
            cfg, "remarketing_meta_window_hours", 24
        ),
        "meta_webhook_enabled": bool(getattr(cfg, "meta_webhook_enabled", False)),
        "meta_app_secret_configured": bool(
            str(getattr(cfg, "meta_app_secret", "") or "").strip()
        ),
        "meta_ig_app_secret_configured": bool(
            str(getattr(cfg, "meta_ig_app_secret", "") or "").strip()
        ),
        "meta_verify_token_configured": bool(
            str(getattr(cfg, "meta_verify_token", "") or "").strip()
        ),
        "meta_ig_secret_same_as_app_secret": (
            bool(str(getattr(cfg, "meta_ig_app_secret", "") or "").strip())
            and str(getattr(cfg, "meta_ig_app_secret", "") or "").strip()
            == str(getattr(cfg, "meta_app_secret", "") or "").strip()
        ),
        "meta_ig_graph": meta_graph,
        "rollout": build_rollout_status(cfg),
    }


@router.get("/")
async def root():
    settings = resolve("get_settings", get_settings)()
    return {
        "ok": True,
        "service": settings.app_name,
        "dry_run": settings.dry_run,
        "environment": settings.environment,
    }


@router.get("/api/health")
async def health():
    return public_health_payload()


@admin_router.get("/health", dependencies=[Depends(verify_admin_token)])
async def admin_health():
    return await admin_diagnostics_payload()


@admin_router.get("/rollout", dependencies=[Depends(verify_admin_token)])
async def admin_rollout_status():
    return {"ok": True, **build_rollout_status()}


@router.get("/api/integrations/tray/test", dependencies=[Depends(verify_admin_token)])
async def test_tray_integration():
    client_cls = resolve("TrayAdapterClient", TrayAdapterClient)
    try:
        await client_cls().search_products(limit=1)
    except TrayAdapterError as exc:
        print("[tray.integration] diagnostic_failed", {"status_code": exc.status_code})
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "tray_adapter_connected": False,
                "products_accessible": False,
                "error": "tray_adapter_unavailable",
            },
        )
    return {
        "success": True,
        "tray_adapter_connected": True,
        "products_accessible": True,
    }
