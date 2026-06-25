from __future__ import annotations
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.security import verify_brevo_webhook, verify_admin_token
from app.webhook_parser import parse_brevo_whatsapp_payload
from app.repository import find_customer_profile_by_phone
from app.openai_agent import generate_agent_reply
from app.brevo_client import send_whatsapp_reply
from app.db import insert_inbound_message, insert_agent_response
from app.config import get_settings

app = FastAPI(title="NewStoreAgent Webhook", version="1.0.0")


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
    return {
        "ok": True,
        "openai_configured": bool(settings.openai_api_key),
        "database_configured": bool(settings.database_url),
        "brevo_send_configured": bool(settings.brevo_send_url and settings.brevo_api_key),
        "dry_run": settings.dry_run,
    }


@app.post("/api/webhooks/brevo/whatsapp")
async def brevo_whatsapp_webhook(request: Request, _: None = Depends(verify_brevo_webhook)):
    payload = await request.json()
    incoming = parse_brevo_whatsapp_payload(payload)

    inbound_id = insert_inbound_message(incoming.model_dump())
    customer_context = find_customer_profile_by_phone(incoming.sender_phone)
    agent_result = generate_agent_reply(incoming, customer_context)
    send_result = await send_whatsapp_reply(incoming.sender_phone, agent_result.reply_text)

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
    payload = await request.json()
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
