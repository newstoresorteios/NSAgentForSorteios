from __future__ import annotations

import json
from json import JSONDecodeError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.security import verify_brevo_webhook, verify_admin_token
from app.webhook_parser import parse_brevo_whatsapp_payload, should_skip_auto_reply
from app.repository import find_customer_profile_by_phone
from app.openai_agent import generate_agent_reply
from app.brevo_client import send_brevo_reply
from app.db import insert_inbound_message, insert_agent_response
from app.config import get_settings

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


@app.get("/api/health")
async def health():
    settings = get_settings()
    openai_key = settings.openai_api_key
    return {
        "ok": True,
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
        "dry_run": settings.dry_run,
    }


@app.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(request: Request, _: None = Depends(verify_brevo_webhook)):
    payload = await read_request_payload(request)

    print("[brevo.webhook] received", {
        "content_type": request.headers.get("content-type"),
        "has_body": True,
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "event": payload.get("eventName") or payload.get("event") if isinstance(payload, dict) else None,
    })

    if should_skip_auto_reply(payload):
        return JSONResponse({"ok": True, "skipped": True, "reason": "no_visitor_message"})

    incoming = parse_brevo_whatsapp_payload(payload)

    print("[brevo.webhook] parsed", {
        "event_type": incoming.event_type,
        "has_visitor_id": bool(incoming.visitor_id),
        "has_sender_phone": bool(incoming.sender_phone),
        "has_text": bool(incoming.text),
    })

    if not incoming.text.strip():
        return JSONResponse({"ok": True, "skipped": True, "reason": "empty_text"})

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
    agent_result = generate_agent_reply(incoming, customer_context)
    send_result = await send_brevo_reply(incoming, agent_result.reply_text)

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
    agent_result = generate_agent_reply(incoming, customer_context)
    return {
        "ok": True,
        "reply_text": agent_result.reply_text,
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
