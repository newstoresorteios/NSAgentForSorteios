from __future__ import annotations

import json
import hashlib
from json import JSONDecodeError
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.security import verify_brevo_webhook, verify_admin_token, verify_remarketing_cron
from app.persona_admin_api import router as persona_admin_router
from app.pix_webhook_api import router as pix_payments_router
from app.webhook_parser import (
    inbound_skip_reason,
    parse_brevo_conversations_payload,
    select_effective_inbound_message,
    selected_message_info,
    webhook_event_skip_reason,
)
from app.repository import find_customer_profile_by_phone
from app.message_pipeline import process_incoming_message
from app.brevo_client import send_brevo_reply
from app.db import (
    claim_inbound_message,
    has_successful_agent_response,
    inbound_message_exists,
    insert_agent_response,
    insert_inbound_message,
    is_latest_inbound_message,
)
from app.inbound_coalesce import is_caption_echo_of_recent_image
from app.config import get_allowed_channels, get_settings
from app.rollout import build_rollout_status
from app.conversation_lock import (
    ConversationLockUnavailable,
    acquire_conversation_lock,
    conversation_lock_key,
    release_conversation_lock,
)
from app.handoff_service import handoff_provider_payload
from app.remarketing import run_remarketing_batch, sync_remarketing_interaction
from app.product_image_index import run_product_image_index_batch
from app.runtime_context import (
    get_current_turn,
    reset_current_turn,
    runtime_stage,
    set_current_turn,
)
from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError
from app.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.observability import (
    log_event,
    log_exception,
    redact_text,
    summarize_webhook_payload,
)

app = FastAPI(title="NewStoreAgent Webhook", version="1.0.0")
app.include_router(persona_admin_router)
app.include_router(pix_payments_router)


def _request_trace_id(request: Request) -> str:
    supplied = (request.headers.get("x-request-id") or "").strip()
    if supplied and len(supplied) <= 64 and all(
        char.isalnum() or char in {"-", "_"} for char in supplied
    ):
        return supplied
    return uuid4().hex


@app.middleware("http")
async def turn_runtime_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    monitored_path = path.startswith(("/api/webhooks/", "/api/test/agent"))
    http_obs = bool(getattr(settings, "agent_http_obs_logs", False))
    runtime_enabled = bool(getattr(settings, "agent_runtime_enabled", True))

    if not runtime_enabled and not http_obs:
        return await call_next(request)

    # Always attach a turn context for webhook/test; optionally for every route.
    attach_turn = runtime_enabled and (monitored_path or http_obs)
    if not attach_turn:
        return await call_next(request)

    from app.llm_call_policy import build_llm_call_budget

    budget_cfg = build_llm_call_budget(execution_path="normal")
    context = TurnRuntimeContext(
        trace_id=_request_trace_id(request),
        llm_budget=LLMCallBudget(
            max_calls=int(budget_cfg.get("max_calls") or 2),
            enforce=bool(budget_cfg.get("enforce", True)),
        ),
    )
    context.execution_path = str(budget_cfg.get("execution_path") or "normal")
    context.start_stage("request")
    token = set_current_turn(context)
    status_code = 500
    try:
        raw_query = str(request.url.query or "")
        safe_query = None
        if raw_query:
            # Never log webhook/admin tokens even when http obs is on.
            import re as _re

            safe_query = _re.sub(
                r"(?i)(token|secret|api_key|apikey|authorization)=([^&]*)",
                r"\1=[REDACTED]",
                raw_query,
            )[:200]
        log_event(
            "http.request",
            {
                "method": request.method,
                "path": path,
                "query": safe_query,
                "content_type": request.headers.get("content-type"),
                "user_agent": (request.headers.get("user-agent") or "")[:120] or None,
                "monitored_path": monitored_path,
            },
        )
        response = await call_next(request)
        status_code = getattr(response, "status_code", 200) or 200
        response.headers["X-Trace-ID"] = context.trace_id
        return response
    except Exception as exc:
        if not isinstance(exc, HTTPException):
            log_exception(
                "http.exception",
                exc,
                {"method": request.method, "path": path},
            )
        raise
    finally:
        await release_conversation_lock(
            getattr(request.state, "conversation_lock_handle", None)
        )
        context.finish_stage("request")
        summary = context.safe_summary()
        log_event(
            "http.response",
            {
                "method": request.method,
                "path": path,
                "status_code": status_code,
                "latency_ms": context.stage_durations_ms.get("request"),
            },
        )
        if monitored_path:
            print("[agent.runtime]", summary)
            log_event("runtime.summary", summary)
        reset_current_turn(token)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_exception(
        "app.unhandled_exception",
        exc,
        {
            "method": request.method,
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error"},
    )


def _webhook_event_name(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = (
        payload.get("eventName")
        or payload.get("event")
        or payload.get("eventType")
    )
    return str(value) if value is not None else None


def _skip_webhook_event(
    *,
    event_name: str | None,
    reason: str,
    error_type: str | None = None,
) -> JSONResponse:
    skipped = {
        "event_name": event_name,
        "should_process": False,
        "reason": reason,
    }
    if error_type is not None:
        skipped["error_type"] = error_type
    log_event("brevo.webhook.routing", skipped)
    log_event("brevo.webhook.skipped", skipped)
    return JSONResponse({"ok": True, "skipped": True, "reason": reason})


async def read_request_payload(request: Request) -> dict:
    """Read request body defensively.

    Accepts:
    - application/json
    - x-www-form-urlencoded with payload/data/body/json containing JSON
    - plain JSON body

    Never lets JSONDecodeError crash the ASGI app.
    """
    raw_body = await request.body()

    if not raw_body:
        return {}

    content_type = (request.headers.get("content-type") or "").lower()
    raw_text = raw_body.decode("utf-8", errors="replace").strip()

    # 1) JSON direto
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except JSONDecodeError:
        pass

    # 2) Form-data / x-www-form-urlencoded
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            form_data = dict(form)

            for key in ("payload", "data", "body", "json"):
                value = form_data.get(key)
                if isinstance(value, str) and value.strip():
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, dict):
                            return parsed
                    except JSONDecodeError:
                        continue

            return form_data
        except Exception:
            pass

    # 3) Erro controlado, sem derrubar ASGI/Vercel
    raise HTTPException(
        status_code=400,
        detail={
            "error": "invalid_json_body",
            "message": "O body recebido não é um JSON válido.",
            "content_type": content_type,
            "raw_preview": raw_text[:200],
            "hint": "Envie Content-Type: application/json com propriedades entre aspas duplas.",
        },
    )


@app.get("/")
async def root():
    settings = get_settings()
    return {
        "ok": True,
        "service": settings.app_name,
        "dry_run": settings.dry_run,
        "environment": settings.environment,
    }


AGENT_VERSION = "openai-db-context-multichannel-runtime-v7"


@app.get("/api/health")
async def health():
    settings = get_settings()
    openai_key = settings.openai_api_key
    allowed_channels = get_allowed_channels(settings)
    ordered_channels = [
        channel
        for channel in ("whatsapp", "instagram", "facebook")
        if channel in allowed_channels
    ]
    ordered_channels.extend(sorted(allowed_channels.difference(ordered_channels)))
    return {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "agent_mode": "openai_with_db_context",
        "openai_configured": bool(openai_key),
        "openai_key_format_ok": openai_key.startswith(("sk-", "sk-proj-")),
        "openai_key_length": len(openai_key),
        "openai_model": settings.openai_model,
        "agent_runtime_enabled": getattr(settings, "agent_runtime_enabled", True),
        "agent_full_obs_logs": getattr(settings, "agent_full_obs_logs", False),
        "agent_http_obs_logs": getattr(settings, "agent_http_obs_logs", False),
        "agent_llm_budget_enabled": getattr(
            settings,
            "agent_llm_budget_enabled",
            False,
        ),
        "agent_max_llm_calls_per_turn": getattr(
            settings,
            "agent_max_llm_calls_per_turn",
            2,
        ),
        "agent_policy_mode": getattr(settings, "agent_policy_mode", "shadow"),
        "agent_factual_validation_mode": getattr(
            settings,
            "agent_factual_validation_mode",
            "shadow",
        ),
        "agent_conversation_lock_enabled": getattr(
            settings,
            "agent_conversation_lock_enabled",
            True,
        ),
        "agent_quality_judge_mode": getattr(
            settings,
            "agent_quality_judge_mode",
            "shadow",
        ),
        "agent_critique_mode": getattr(settings, "agent_critique_mode", "enforce"),
        "agent_critique_max_retries": getattr(
            settings,
            "agent_critique_max_retries",
            2,
        ),
        "agent_history_limit": getattr(settings, "agent_history_limit", 80),
        "agent_send_idempotency_enabled": getattr(
            settings,
            "agent_send_idempotency_enabled",
            True,
        ),
        "database_configured": bool(settings.database_url),
        "sorteio_database_configured": bool(
            getattr(settings, "sorteio_database_url", "") or settings.database_url
        ),
        "sorteio_database_dedicated": bool(
            str(getattr(settings, "sorteio_database_url", "") or "").strip()
        ),
        "brevo_send_configured": bool(
            settings.brevo_api_key
            and (
                settings.brevo_agent_id
                or (settings.brevo_agent_email and settings.brevo_agent_name)
                or settings.brevo_sender_number
            )
        ),
        "brevo_conversations_configured": bool(
            settings.brevo_api_key
            and (
                settings.brevo_agent_id
                or (settings.brevo_agent_email and settings.brevo_agent_name)
            )
        ),
        "brevo_whatsapp_configured": bool(
            settings.brevo_api_key and settings.brevo_sender_number
        ),
        "brevo_social_channels_enabled": getattr(settings, "brevo_social_channels_enabled", True),
        "brevo_allowed_channels": ordered_channels,
        "brevo_reply_mode": settings.brevo_reply_mode,
        "brevo_live_send_enabled": (not settings.dry_run and settings.brevo_reply_mode.lower() != "dry_run"),
        "brevo_webhook_secret_configured": bool(settings.brevo_webhook_secret),
        "audio_inbound_enabled": settings.audio_inbound_enabled,
        "audio_outbound_enabled": settings.audio_outbound_enabled,
        "supabase_storage_configured": bool(settings.supabase_url and settings.supabase_service_key),
        "dry_run": settings.dry_run,
        "tray_adapter_configured": bool(settings.tray_adapter_url and settings.tray_adapter_token),
        "tray_tools_enabled": bool(settings.tray_adapter_url and settings.tray_adapter_token),
        "pix_direct_enabled": bool(getattr(settings, "pix_direct_enabled", False)),
        "pix_mp_configured": bool(
            (
                getattr(settings, "resolved_mp_access_token", None)()
                if callable(getattr(settings, "resolved_mp_access_token", None))
                else (
                    getattr(settings, "mp_access_token", "")
                    or getattr(settings, "mercadopago_access_token", "")
                )
            )
        ),
        "pix_public_url_configured": bool(getattr(settings, "public_url", "")),
        "pix_webhook_path": "/api/payments/webhook",
        "remarketing_enabled": getattr(settings, "remarketing_enabled", False),
        "remarketing_cron_configured": bool(
            getattr(settings, "remarketing_cron_secret", "")
        ),
        "remarketing_touch_hours": getattr(
            settings,
            "remarketing_touch_hours",
            "1,12,23",
        ),
        "remarketing_meta_window_hours": getattr(
            settings,
            "remarketing_meta_window_hours",
            24,
        ),
        "rollout": build_rollout_status(settings),
    }


@app.get("/api/integrations/tray/test", dependencies=[Depends(verify_admin_token)])
async def test_tray_integration():
    try:
        await TrayAdapterClient().search_products(limit=1)
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


async def handle_brevo_conversations_webhook(request: Request) -> JSONResponse:
    try:
        payload = await read_request_payload(request)
    except HTTPException:
        log_event(
            "brevo.webhook.routing",
            {
                "event_name": None,
                "should_process": False,
                "reason": "invalid_payload",
            },
        )
        log_event(
            "brevo.webhook.skipped",
            {"event_name": None, "reason": "invalid_payload"},
        )
        raise

    event_name = _webhook_event_name(payload)
    log_event(
        "brevo.webhook.received",
        {
            "content_type": request.headers.get("content-type"),
            "has_body": True,
            **summarize_webhook_payload(payload if isinstance(payload, dict) else {}),
        },
    )

    event_skip_reason = webhook_event_skip_reason(payload)
    if event_skip_reason:
        return _skip_webhook_event(
            event_name=event_name,
            reason=event_skip_reason,
        )

    try:
        incoming = parse_brevo_conversations_payload(payload)
    except Exception as exc:
        log_exception(
            "brevo.webhook.parse_failed",
            exc,
            {
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "parsed": False,
            },
        )
        log_event(
            "brevo.webhook.parsed",
            {
                "parsed": False,
                "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
                "channel": "unknown",
                "sender_key_present": False,
                "visitor_id_present": False,
                "source_conversation_ref_present": False,
                "message_id_present": False,
                "conversation_id_present": False,
                "sender_phone_present": False,
                "text_present": False,
                "input_modality": None,
                "attachment_type": None,
                "direction": None,
            },
        )
        return _skip_webhook_event(
            event_name=event_name,
            reason="invalid_payload",
            error_type=type(exc).__name__,
        )

    runtime = get_current_turn()
    if runtime is not None:
        runtime.channel = incoming.channel
        runtime.conversation_key = (
            incoming.conversation_id
            or incoming.sender_key
            or incoming.visitor_id
            or incoming.sender_phone
            or "unresolved"
        )

    log_event(
        "brevo.webhook.parsed",
        {
            "parsed": True,
            "event_name": incoming.event_type,
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "message_id_present": bool(incoming.message_id),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "sender_name_present": bool(incoming.sender_name),
            "text_present": bool(incoming.text),
            "text_chars": len(incoming.text or ""),
            "text_preview": redact_text(incoming.text, max_chars=200),
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "image_url_present": bool((incoming.image_url or "").strip()),
            "audio_url_present": bool(incoming.audio_url),
            "direction": selected_message_info(payload).get("role"),
        },
    )

    selected = select_effective_inbound_message(payload)
    selection_info = selected_message_info(payload, selected)
    log_event(
        "brevo.webhook.selected_message",
        {
            "message_id_present": bool(incoming.message_id),
            "role": selection_info.get("role"),
            "timestamp_present": selection_info.get("timestamp_present"),
            "text_length": len(incoming.text or ""),
            "text_hash": hashlib.sha256((incoming.text or "").encode("utf-8")).hexdigest()[:12],
            "ordering_fallback": selection_info.get("ordering_fallback"),
            "channel": incoming.channel,
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
        },
    )

    skip_reason = inbound_skip_reason(payload)
    if skip_reason:
        if skip_reason in {"agent_message", "outbound_message"}:
            try:
                from app.human_takeover import touch_human_activity

                touch_human_activity(incoming)
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "brevo.webhook.human_takeover_touch_failed",
                    exc,
                    {"skip_reason": skip_reason},
                )
        return _skip_webhook_event(
            event_name=event_name,
            reason=skip_reason,
        )

    settings = get_settings()
    allowed_channels = get_allowed_channels(settings)
    if incoming.channel not in allowed_channels:
        return _skip_webhook_event(
            event_name=event_name,
            reason="channel_not_allowed",
        )
    if incoming.channel in {"instagram", "facebook"} and not settings.brevo_social_channels_enabled:
        return _skip_webhook_event(
            event_name=event_name,
            reason="social_channels_disabled",
        )

    if not any((incoming.sender_key, incoming.visitor_id, incoming.conversation_id)):
        return _skip_webhook_event(
            event_name=event_name,
            reason="missing_sender_identity",
        )

    if (
        not incoming.text.strip()
        and not incoming.audio_url
        and not (incoming.image_url or "").strip()
    ):
        return _skip_webhook_event(
            event_name=event_name,
            reason="no_text",
        )

    # Cheap duplicate check before waiting on the conversation lock. Brevo often
    # redelivers the same fragment while Vision/catalog still holds the lock.
    if incoming.message_id and inbound_message_exists(incoming.provider, incoming.message_id):
        return _skip_webhook_event(
            event_name=event_name,
            reason="duplicate_message",
        )

    if getattr(settings, "agent_conversation_lock_enabled", True):
        lock_key = conversation_lock_key(
            conversation_id=incoming.conversation_id,
            sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone,
            visitor_id=incoming.visitor_id,
        )
        if lock_key:
            lock_timeout = float(
                getattr(
                    settings,
                    "agent_conversation_lock_timeout_seconds",
                    15.0,
                )
            )
            # Keep waits short: the in-flight turn owns the work. Returning 503
            # makes Brevo retry and amplifies lock contention during photo turns.
            if (incoming.image_url or "").strip() or (
                (incoming.attachment_type or "").lower() == "image"
            ):
                lock_timeout = min(max(lock_timeout, 5.0), 12.0)
            try:
                with runtime_stage("conversation_lock_wait"):
                    request.state.conversation_lock_handle = (
                        await acquire_conversation_lock(
                            lock_key,
                            database_url=getattr(
                                settings,
                                "database_url",
                                "",
                            ),
                            timeout_seconds=lock_timeout,
                        )
                    )
            except ConversationLockUnavailable as exc:
                lock_error = str(exc)
                log_event(
                    "agent.lock.unavailable",
                    {
                        "error": lock_error,
                        "channel": incoming.channel,
                        "conversation_id_present": bool(
                            incoming.conversation_id
                        ),
                        "sender_key_present": bool(incoming.sender_key),
                        "inbound_image": bool((incoming.image_url or "").strip()),
                        "lock_timeout_seconds": lock_timeout,
                    },
                )
                # Contention: another worker holds the conversation — acknowledge
                # and stop Brevo retries. DB lock failure is different: dropping
                # the event loses photo turns, so fall back to a local-only lock.
                if lock_error == "database_lock_unavailable":
                    try:
                        request.state.conversation_lock_handle = (
                            await acquire_conversation_lock(
                                lock_key,
                                database_url="",
                                timeout_seconds=min(lock_timeout, 5.0),
                            )
                        )
                        log_event(
                            "agent.lock.local_fallback",
                            {
                                "channel": incoming.channel,
                                "inbound_image": bool(
                                    (incoming.image_url or "").strip()
                                ),
                            },
                        )
                    except ConversationLockUnavailable:
                        return _skip_webhook_event(
                            event_name=event_name,
                            reason="conversation_busy",
                        )
                else:
                    return _skip_webhook_event(
                        event_name=event_name,
                        reason="conversation_busy",
                    )

    # Brevo often redelivers the caption as a second text-only webhook after the
    # photo+caption turn. Skip exact caption echoes so we don't reply twice.
    if not (incoming.image_url or "").strip() and is_caption_echo_of_recent_image(
        incoming
    ):
        return _skip_webhook_event(
            event_name=event_name,
            reason="caption_echo",
        )

    try:
        claimed, inbound_id = claim_inbound_message(incoming.model_dump())
    except Exception as exc:
        log_exception(
            "brevo.webhook.inbound_insert_failed",
            exc,
            {
                "event_type": incoming.event_type,
                "channel": incoming.channel,
                "sender_key_present": bool(incoming.sender_key),
                "visitor_id_present": bool(incoming.visitor_id),
                "conversation_id_present": bool(incoming.conversation_id),
                "sender_phone_present": bool(incoming.sender_phone),
                "message_id_present": bool(incoming.message_id),
                "input_modality": incoming.input_modality,
                "attachment_type": incoming.attachment_type,
            },
        )
        raise HTTPException(status_code=500, detail={"error": "inbound_insert_failed"}) from exc
    if not claimed:
        return _skip_webhook_event(
            event_name=event_name,
            reason="duplicate_message",
        )

    # Central ChatBô: se um humano assumiu, grava inbound mas não responde.
    try:
        from app.human_takeover import human_takeover_active

        if human_takeover_active(incoming):
            return _skip_webhook_event(
                event_name=event_name,
                reason="human_takeover",
            )
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "brevo.webhook.human_takeover_check_failed",
            exc,
            {"inbound_id": inbound_id},
        )

    if runtime is not None:
        runtime.inbound_id = inbound_id
    if isinstance(incoming.raw, dict):
        incoming.raw["inbound_id"] = inbound_id
    if incoming.sender_phone:
        customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    else:
        customer_context = {
            "found": False,
            "channel": incoming.channel,
            "sender_key": incoming.sender_key,
            "display_name": incoming.sender_name,
        }
    log_event(
        "brevo.webhook.routing",
        {
            "event_name": event_name,
            "should_process": True,
            "reason": "inbound_message",
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    log_event(
        "brevo.webhook.processing",
        {
            "channel": incoming.channel,
            "sender_key_present": bool(incoming.sender_key),
            "visitor_id_present": bool(incoming.visitor_id),
            "source_conversation_ref_present": bool(incoming.source_conversation_ref),
            "conversation_id_present": bool(incoming.conversation_id),
            "sender_phone_present": bool(incoming.sender_phone),
            "message_id_present": bool(incoming.message_id),
            "event_name": incoming.event_type,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "text_preview": redact_text(incoming.text, max_chars=200),
            "inbound_id": inbound_id,
            "customer_found": bool(customer_context.get("found")),
        },
    )
    agent_result = await process_incoming_message(incoming, customer_context)

    log_event(
        "brevo.webhook.agent_result",
        {
            "intent": agent_result.intent,
            "handoff_required": agent_result.handoff_required,
            "safety_reason": agent_result.safety_reason,
            "reply_length": len(agent_result.reply_text or ""),
            "reply_preview": redact_text(agent_result.reply_text, max_chars=200),
            "channel": incoming.channel,
            "input_modality": incoming.input_modality,
            "attachment_type": incoming.attachment_type,
            "transcription_failed": incoming.transcription_failed,
            "response_source": (agent_result.response_metadata or {}).get(
                "response_source"
            ),
            "domain": (agent_result.response_metadata or {}).get("domain"),
            "goal": (agent_result.response_metadata or {}).get("goal"),
            "used_openai_interpreter": bool(
                (agent_result.response_metadata or {}).get("used_openai_interpreter")
            ),
            "used_openai_responder": bool(
                (agent_result.response_metadata or {}).get("used_openai_responder")
            ),
            "used_tray": bool((agent_result.response_metadata or {}).get("used_tray")),
        },
    )

    if not is_latest_inbound_message(
        inbound_id,
        incoming.conversation_id,
        incoming.sender_key,
        incoming.sender_phone,
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {"reason": "stale_inbound", "inbound_id": inbound_id},
        )
        send_result = None
        provider_send_ok = False
        provider_response = {"skipped": True, "reason": "stale_inbound"}
    elif (
        getattr(settings, "agent_send_idempotency_enabled", True)
        and has_successful_agent_response(inbound_id)
    ):
        log_event(
            "brevo.webhook.skipped_reply",
            {
                "reason": "already_sent",
                "inbound_id": inbound_id,
            },
        )
        send_result = None
        provider_send_ok = True
        provider_response = {"skipped": True, "reason": "already_sent"}
    else:
        send_result = await send_brevo_reply(incoming, agent_result)
        provider_send_ok = send_result.ok
        provider_response = send_result.model_dump()
        log_event(
            "brevo.webhook.send_result",
            {
                "ok": send_result.ok,
                "dry_run": send_result.dry_run,
                "status_code": send_result.status_code,
                "error": send_result.error,
                "channel": incoming.channel,
                "inbound_id": inbound_id,
                "reply_chars": len(agent_result.reply_text or ""),
            },
        )
    commerce_state = (agent_result.response_metadata or {}).get("commerce_state")
    decision_snapshot = (agent_result.response_metadata or {}).get(
        "decision_snapshot"
    )
    factual_validation = (agent_result.response_metadata or {}).get(
        "factual_validation"
    )
    quality_judge = (agent_result.response_metadata or {}).get("quality_judge")
    handoff_payload = handoff_provider_payload(agent_result)
    runtime_summary = None
    if runtime is not None:
        runtime.register_fallback(
            (agent_result.response_metadata or {}).get("fallback_reason")
            or agent_result.safety_reason
        )
        runtime_summary = runtime.safe_summary()
        agent_result.response_metadata["turn_runtime"] = runtime_summary
    if isinstance(provider_response, dict):
        agent_context: dict[str, object] = {}
        if isinstance(commerce_state, dict):
            agent_context["commerce_state"] = commerce_state
        if isinstance(decision_snapshot, dict):
            agent_context["decision_snapshot"] = decision_snapshot
        if isinstance(factual_validation, dict):
            agent_context["factual_validation"] = factual_validation
        if isinstance(quality_judge, dict):
            agent_context["quality_judge"] = quality_judge
        if isinstance(handoff_payload, dict):
            agent_context["handoff"] = handoff_payload
        if agent_context:
            provider_response["_agent_context"] = agent_context
    if isinstance(provider_response, dict) and isinstance(runtime_summary, dict):
        provider_response["_agent_runtime"] = runtime_summary

    try:
        insert_agent_response(
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
                "sender_key": incoming.sender_key,
                "sender_phone": incoming.sender_phone,
                "reply_text": agent_result.reply_text,
                "intent": agent_result.intent,
                "handoff_required": agent_result.handoff_required,
                "safety_reason": agent_result.safety_reason,
                "provider_send_ok": provider_send_ok,
                "provider_response": provider_response,
            }
        )
    except Exception as exc:
        log_exception(
            "brevo.webhook.response_insert_failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "response_insert_failed",
                "message": "Falha ao registrar resposta do agente.",
            },
        ) from exc

    try:
        sync_remarketing_interaction(
            incoming,
            inbound_id=inbound_id,
            response_metadata=(
                agent_result.response_metadata
                if provider_send_ok
                else {}
            ),
            handoff_required=agent_result.handoff_required,
        )
    except Exception as exc:
        log_exception(
            "remarketing.sync.failed",
            exc,
            {
                "inbound_id": inbound_id,
                "channel": incoming.channel,
            },
        )

    return JSONResponse(
        {
            "ok": True,
            "inbound_id": inbound_id,
            "reply_dry_run": send_result.dry_run if send_result else False,
            "reply_sent": send_result.ok if send_result else False,
            "handoff_required": agent_result.handoff_required,
            "skipped_reply": not bool(send_result),
        }
    )


@app.get(
    "/api/cron/remarketing",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def remarketing_cron():
    result = await run_remarketing_batch()
    log_event("remarketing.cron.completed", result if isinstance(result, dict) else {"result": result})
    return {"ok": True, **result}


@app.post(
    "/api/cron/remarketing",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def remarketing_cron_manual():
    return await remarketing_cron()


@app.get(
    "/api/cron/product-image-index",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def product_image_index_cron():
    result = await run_product_image_index_batch()
    log_event(
        "product_image_index.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


@app.post(
    "/api/cron/product-image-index",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def product_image_index_cron_manual():
    return await product_image_index_cron()


@app.get(
    "/api/cron/attendance-learning",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def attendance_learning_cron():
    from app.attendance_learning import run_attendance_learning_batch

    result = await run_attendance_learning_batch()
    log_event(
        "attendance.learning.cron.completed",
        result if isinstance(result, dict) else {"result": result},
    )
    return result


@app.post(
    "/api/cron/attendance-learning",
    dependencies=[Depends(verify_remarketing_cron)],
)
async def attendance_learning_cron_manual():
    return await attendance_learning_cron()


@app.post("/api/webhooks/brevo/conversations")
async def brevo_conversations_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)


@app.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(
    request: Request,
    _: None = Depends(verify_brevo_webhook),
):
    return await handle_brevo_conversations_webhook(request)


@app.get("/api/admin/rollout", dependencies=[Depends(verify_admin_token)])
async def admin_rollout_status():
    """Read-only rollout snapshot + rollback checklist (mutate via Vercel env)."""
    return {"ok": True, **build_rollout_status()}


@app.get("/api/admin/instagram/stories", dependencies=[Depends(verify_admin_token)])
async def admin_list_instagram_stories(
    tenant_id: str = "newstore",
    status: str | None = None,
    instagram_account_id: str | None = None,
    product_id: str | None = None,
    limit: int = 50,
):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    rows = StoryProductRepository().list_stories(
        tenant_id=tenant_id,
        status=status,
        instagram_account_id=instagram_account_id,
        product_id=product_id,
        limit=limit,
    )
    return {
        "ok": True,
        "items": [row.model_dump(mode="json") for row in rows],
    }


@app.get("/api/admin/instagram/stories/{row_id}", dependencies=[Depends(verify_admin_token)])
async def admin_get_instagram_story(row_id: int, tenant_id: str = "newstore"):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    row = StoryProductRepository().get_by_id(tenant_id=tenant_id, row_id=row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/link-product",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_link_instagram_story_product(request: Request):
    from app.config import get_settings
    from app.story_publication_link_service import (
        register_published_story,
        validate_link_payload,
    )

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    try:
        cleaned = validate_link_payload(body)
        # Ignore any client-supplied price/stock keys by construction.
        assoc = register_published_story(**cleaned)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__},
            status_code=500,
        )
    return {"ok": True, "item": assoc.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/{row_id}/confirm",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_confirm_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    tenant_id = str(body.get("tenant_id") or "newstore")
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=tenant_id, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    product_id = str(body.get("product_id") or "").strip()
    catalog_item_key = str(body.get("catalog_item_key") or "").strip()
    if not product_id or not catalog_item_key:
        return JSONResponse({"ok": False, "error": "product_required"}, status_code=400)
    confirmed = repo.confirm_match(
        tenant_id=tenant_id,
        provider=existing.provider,
        instagram_account_id=existing.instagram_account_id,
        story_media_id=existing.story_media_id,
        catalog_item_key=catalog_item_key,
        product_id=product_id,
        variant_id=(
            str(body["variant_id"]) if body.get("variant_id") not in (None, "") else None
        ),
        match_source="manual",
        match_confidence=1.0,
        match_status="manually_confirmed",
        confirmed_by=str(body.get("confirmed_by") or "admin"),
        explanation={
            "reason": str(body.get("reason") or "manual_confirm")[:200],
            "previous_product_id": existing.product_id,
        },
    )
    return {"ok": True, "item": confirmed.model_dump(mode="json") if confirmed else None}


@app.post(
    "/api/admin/instagram/stories/{row_id}/unlink",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_unlink_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    tenant_id = str((body or {}).get("tenant_id") or "newstore")
    row = StoryProductRepository().unlink(
        tenant_id=tenant_id,
        row_id=row_id,
        confirmed_by=str((body or {}).get("confirmed_by") or "admin"),
    )
    if row is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return {"ok": True, "item": row.model_dump(mode="json")}


@app.post(
    "/api/admin/instagram/stories/{row_id}/reprocess",
    dependencies=[Depends(verify_admin_token)],
)
async def admin_reprocess_instagram_story(row_id: int, request: Request):
    from app.config import get_settings
    from app.story_product_repository import StoryProductRepository

    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_admin_api_enabled", True)):
        return JSONResponse({"ok": False, "error": "admin_api_disabled"}, status_code=403)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    tenant_id = str((body or {}).get("tenant_id") or "newstore")
    repo = StoryProductRepository()
    existing = repo.get_by_id(tenant_id=tenant_id, row_id=row_id)
    if existing is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    reset = repo.unlink(
        tenant_id=tenant_id,
        row_id=row_id,
        confirmed_by=str((body or {}).get("confirmed_by") or "admin_reprocess"),
    )
    return {
        "ok": True,
        "item": reset.model_dump(mode="json") if reset else None,
        "note": "Association reset to pending; next customer reply will re-analyze once.",
    }


@app.post("/api/test/agent")
async def test_agent(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)

    log_event(
        "agent.test.received",
        {
            "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            "has_phone": bool(payload.get("phone")) if isinstance(payload, dict) else False,
            "has_text": bool(payload.get("text")) if isinstance(payload, dict) else False,
            "text_preview": redact_text(
                str(payload.get("text") or "") if isinstance(payload, dict) else None,
                max_chars=120,
            )
            if isinstance(payload, dict)
            else None,
        },
    )

    incoming = parse_brevo_conversations_payload(
        {
            "text": payload.get("text", "Olá"),
            "from": payload.get("phone"),
            "name": payload.get("name", "Teste"),
            "event": "manual_test",
        }
    )
    customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    agent_result = await process_incoming_message(incoming, customer_context)
    return {
        "ok": True,
        "reply_text": agent_result.reply_text,
        "reply_modality": agent_result.reply_modality,
        "input_modality": incoming.input_modality,
        "transcribed_text": incoming.text if incoming.input_modality == "audio" else None,
        "intent": agent_result.intent,
        "handoff_required": agent_result.handoff_required,
        "safety_reason": agent_result.safety_reason,
        "customer_context": customer_context,
    }


@app.post("/api/debug/echo")
async def debug_echo(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)
    return {
        "ok": True,
        "content_type": request.headers.get("content-type"),
        "keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "payload": payload,
    }
