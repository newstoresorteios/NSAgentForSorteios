from __future__ import annotations

import json
from json import JSONDecodeError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.security import verify_brevo_webhook, verify_admin_token
from app.webhook_parser import inbound_skip_reason, parse_brevo_whatsapp_payload
from app.repository import find_customer_profile_by_phone
from app.message_pipeline import process_incoming_message
from app.brevo_client import send_brevo_reply
from app.db import inbound_message_exists, insert_inbound_message, insert_agent_response
from app.config import get_settings
from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError

app = FastAPI(title="NewStoreAgent Webhook", version="1.0.0")


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


AGENT_VERSION = "openai-db-context-v2"


@app.get("/api/health")
async def health():
    settings = get_settings()
    openai_key = settings.openai_api_key
    return {
        "ok": True,
        "agent_version": AGENT_VERSION,
        "agent_mode": "openai_with_db_context",
        "openai_configured": bool(openai_key),
        "openai_key_format_ok": openai_key.startswith(("sk-", "sk-proj-")),
        "openai_key_length": len(openai_key),
        "openai_model": settings.openai_model,
        "database_configured": bool(settings.database_url),
        "brevo_send_configured": bool(
            settings.brevo_api_key
            and (
                settings.brevo_agent_id
                or (settings.brevo_agent_email and settings.brevo_agent_name)
                or settings.brevo_sender_number
            )
        ),
        "brevo_reply_mode": settings.brevo_reply_mode,
        "brevo_live_send_enabled": (not settings.dry_run and settings.brevo_reply_mode.lower() != "dry_run"),
        "brevo_webhook_secret_configured": bool(settings.brevo_webhook_secret),
        "audio_inbound_enabled": settings.audio_inbound_enabled,
        "audio_outbound_enabled": settings.audio_outbound_enabled,
        "supabase_storage_configured": bool(settings.supabase_url and settings.supabase_service_key),
        "dry_run": settings.dry_run,
        "tray_adapter_configured": bool(settings.tray_adapter_url and settings.tray_adapter_token),
        "tray_tools_enabled": bool(settings.tray_adapter_url and settings.tray_adapter_token),
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


@app.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(request: Request, _: None = Depends(verify_brevo_webhook)):
    try:
        payload = await read_request_payload(request)
    except HTTPException:
        print("[brevo.webhook] skipped", {"reason": "invalid_payload"})
        raise

    print("[brevo.webhook] received", {
        "content_type": request.headers.get("content-type"),
        "has_body": True,
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "event": payload.get("eventName") or payload.get("event") if isinstance(payload, dict) else None,
    })

    try:
        incoming = parse_brevo_whatsapp_payload(payload)
    except Exception as exc:
        print("[brevo.webhook] parsed", {
            "parsed": False,
            "event_name": payload.get("eventName") if isinstance(payload, dict) else None,
            "message_id_present": False,
            "conversation_id_present": False,
            "sender_phone_present": False,
            "text_present": False,
            "direction": None,
        })
        print("[brevo.webhook] skipped", {"reason": "invalid_payload", "error_type": type(exc).__name__})
        return JSONResponse({"ok": True, "skipped": True, "reason": "invalid_payload"})

    print("[brevo.webhook] parsed", {
        "parsed": True,
        "event_name": incoming.event_type,
        "message_id_present": bool(incoming.message_id),
        "conversation_id_present": bool(incoming.conversation_id),
        "sender_phone_present": bool(incoming.sender_phone),
        "text_present": bool(incoming.text),
        "direction": payload.get("direction") or payload.get("type") or payload.get("eventType") or (
            payload.get("messages", [])[-1].get("type")
            if isinstance(payload.get("messages"), list)
            and payload.get("messages")
            and isinstance(payload.get("messages", [])[-1], dict)
            else None
        ),
    })

    skip_reason = inbound_skip_reason(payload)
    if skip_reason:
        print("[brevo.webhook] skipped", {"reason": skip_reason})
        return JSONResponse({"ok": True, "skipped": True, "reason": skip_reason})

    if not incoming.sender_phone:
        print("[brevo.webhook] skipped", {"reason": "missing_sender"})
        return JSONResponse({"ok": True, "skipped": True, "reason": "missing_sender"})

    if incoming.message_id and inbound_message_exists(incoming.provider, incoming.message_id):
        print("[brevo.webhook] skipped", {"reason": "duplicate_message"})
        return JSONResponse({"ok": True, "skipped": True, "reason": "duplicate_message"})

    if not incoming.text.strip() and not incoming.audio_url:
        print("[brevo.webhook] skipped", {"reason": "no_text"})
        return JSONResponse({"ok": True, "skipped": True, "reason": "no_text"})

    try:
        inbound_id = insert_inbound_message(incoming.model_dump())
    except Exception as exc:
        print("[brevo.webhook] inbound_insert_failed", {
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
            "event_type": incoming.event_type,
            "has_sender_phone": bool(incoming.sender_phone),
            "has_text": bool(incoming.text),
        })
        raise HTTPException(
            status_code=500,
            detail={
                "error": "inbound_insert_failed",
                "message": "Falha ao registrar mensagem recebida.",
            },
        ) from exc

    customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    print("[brevo.webhook] processing", {
        "message_id_present": bool(incoming.message_id),
        "event_name": incoming.event_type,
    })
    agent_result = await process_incoming_message(incoming, customer_context)
    send_result = await send_brevo_reply(incoming, agent_result)

    print("[brevo.webhook] agent_result", {
        "intent": agent_result.intent,
        "handoff_required": agent_result.handoff_required,
        "safety_reason": agent_result.safety_reason,
        "reply_preview": agent_result.reply_text[:160],
        "input_modality": incoming.input_modality,
        "transcription_failed": incoming.transcription_failed,
    })

    try:
        insert_agent_response(
            {
                "inbound_id": inbound_id,
                "sender_phone": incoming.sender_phone,
                "reply_text": agent_result.reply_text,
                "intent": agent_result.intent,
                "handoff_required": agent_result.handoff_required,
                "safety_reason": agent_result.safety_reason,
                "provider_send_ok": send_result.ok,
                "provider_response": send_result.model_dump(),
            }
        )
    except Exception as exc:
        print("[brevo.webhook] response_insert_failed", {
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
            "inbound_id": inbound_id,
        })
        raise HTTPException(
            status_code=500,
            detail={
                "error": "response_insert_failed",
                "message": "Falha ao registrar resposta do agente.",
            },
        ) from exc

    return JSONResponse(
        {
            "ok": True,
            "inbound_id": inbound_id,
            "reply_dry_run": send_result.dry_run,
            "reply_sent": send_result.ok,
            "handoff_required": agent_result.handoff_required,
        }
    )


@app.post("/api/test/agent")
async def test_agent(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)

    print("[agent.test] received", {
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "has_phone": bool(payload.get("phone")) if isinstance(payload, dict) else False,
        "has_text": bool(payload.get("text")) if isinstance(payload, dict) else False,
    })

    incoming = parse_brevo_whatsapp_payload(
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
