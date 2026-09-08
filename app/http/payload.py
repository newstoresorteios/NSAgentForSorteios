"""Shared request-body parsing for webhooks and admin tools."""

from __future__ import annotations

import json
from json import JSONDecodeError

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.ops.observability import log_event


def webhook_event_name(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = (
        payload.get("eventName")
        or payload.get("event")
        or payload.get("eventType")
    )
    return str(value) if value is not None else None


def skip_webhook_event(
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
    """Read request body defensively. Never echo the raw body to the client."""
    raw_body = await request.body()
    if not raw_body:
        return {}

    content_type = (request.headers.get("content-type") or "").lower()
    raw_text = raw_body.decode("utf-8", errors="replace").strip()

    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except JSONDecodeError:
        pass

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

    raise HTTPException(
        status_code=400,
        detail={
            "error": "invalid_json_body",
            "message": "O body recebido não é um JSON válido.",
            "content_type": content_type,
            "hint": "Envie Content-Type: application/json com propriedades entre aspas duplas.",
        },
    )
