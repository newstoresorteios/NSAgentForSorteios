"""Admin-only test/debug routes. Never echo bodies or full customer records."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.channels.webhook_parser import parse_brevo_conversations_payload
from app.http.bindings import resolve
from app.http.payload import read_request_payload
from app.identity.repository import find_customer_profile_by_phone as _find_customer_profile_by_phone
from app.message_pipeline import process_incoming_message as _process_incoming_message
from app.ops.observability import log_event, redact_text
from app.security import verify_admin_token

router = APIRouter(tags=["debug"])


@router.post("/api/test/agent")
async def test_agent(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)
    find_customer_profile_by_phone = resolve(
        "find_customer_profile_by_phone", _find_customer_profile_by_phone
    )
    process_incoming_message = resolve("process_incoming_message", _process_incoming_message)

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
    found = bool(isinstance(customer_context, dict) and customer_context.get("found"))
    return {
        "ok": True,
        "reply_text": agent_result.reply_text,
        "reply_modality": agent_result.reply_modality,
        "input_modality": incoming.input_modality,
        "transcribed_text": incoming.text if incoming.input_modality == "audio" else None,
        "intent": agent_result.intent,
        "handoff_required": agent_result.handoff_required,
        "safety_reason": agent_result.safety_reason,
        "customer_found": found,
    }


@router.post("/api/debug/echo")
async def debug_echo(request: Request, _: None = Depends(verify_admin_token)):
    payload = await read_request_payload(request)
    return {
        "ok": True,
        "content_type": request.headers.get("content-type"),
        "keys": list(payload.keys()) if isinstance(payload, dict) else [],
    }
