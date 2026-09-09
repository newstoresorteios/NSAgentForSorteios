"""Shared request-body parsing for webhooks and admin tools."""

from __future__ import annotations

import json
from json import JSONDecodeError

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.ops.observability import log_event


# Webhook payloads carry message metadata and remote-media URLs, not media bytes.
# Keep a single application-level ceiling so chunked requests cannot bypass a
# provider or platform Content-Length limit.
MAX_REQUEST_BODY_BYTES = 1024 * 1024


def _payload_too_large(*, max_bytes: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "error": "request_body_too_large",
            "max_bytes": max_bytes,
        },
    )


async def read_limited_request_body(
    request: Request,
    *,
    max_bytes: int = MAX_REQUEST_BODY_BYTES,
) -> bytes:
    """Read and cache the exact request bytes while enforcing a hard ceiling."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_bytes = int(content_length)
        except (TypeError, ValueError):
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > max_bytes:
            raise _payload_too_large(max_bytes=max_bytes)

    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > max_bytes:
            raise _payload_too_large(max_bytes=max_bytes)
        chunks.extend(chunk)

    body = bytes(chunks)
    # Request.body() uses this same cache. Restoring it keeps request.form()
    # functional after the bounded read without changing the original bytes.
    request._body = body  # type: ignore[attr-defined]  # noqa: SLF001
    return body


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
    raw_body = await read_limited_request_body(request)
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
