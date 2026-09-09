"""Meta Instagram Messaging webhook — verify + enqueue only."""

from __future__ import annotations

import json
from json import JSONDecodeError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_settings as _get_settings
from app.http.bindings import resolve
from app.http.payload import read_limited_request_body
from app.ops.observability import log_event

router = APIRouter(tags=["webhooks-meta"])


async def probe_meta_ig_graph() -> dict:
    from app.channels.meta_instagram import probe_instagram_graph_subscriptions

    try:
        return await probe_instagram_graph_subscriptions()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


@router.get("/api/webhooks/meta")
async def meta_webhook_verify(request: Request):
    """Meta hub challenge for Instagram Messaging subscriptions."""
    from app.channels.meta_instagram import handle_meta_verify_challenge, meta_webhook_enabled

    settings = resolve("get_settings", _get_settings)()
    if not meta_webhook_enabled():
        raise HTTPException(status_code=404, detail={"error": "meta_webhook_disabled"})
    params = request.query_params
    challenge = handle_meta_verify_challenge(
        mode=params.get("hub.mode"),
        verify_token=params.get("hub.verify_token"),
        challenge=params.get("hub.challenge"),
        expected_token=str(getattr(settings, "meta_verify_token", "") or ""),
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail={"error": "meta_verify_failed"})
    return PlainTextResponse(challenge)


@router.post("/api/webhooks/meta")
async def meta_instagram_webhook(request: Request):
    """Accept Meta Instagram Messaging events into the durable inbox.

    Returns 200 after enqueue. Inbox drain is cron/worker — never inline.
    """
    from app.channels.meta_instagram import (
        messaging_event_shapes,
        meta_webhook_enabled,
        parse_meta_instagram_messaging,
        payload_skeleton,
        verify_meta_signatures,
    )
    from app.ingress.inbox import enqueue_inbound

    settings = resolve("get_settings", _get_settings)()
    if not meta_webhook_enabled():
        raise HTTPException(status_code=404, detail={"error": "meta_webhook_disabled"})

    # Limit the raw body before signature work or JSON parsing. The helper
    # returns the unmodified bytes required by Meta's HMAC verification.
    body = await read_limited_request_body(request)
    signature_sha256 = (
        request.headers.get("x-hub-signature-256")
        or request.headers.get("X-Hub-Signature-256")
    )
    signature_sha1 = (
        request.headers.get("x-hub-signature")
        or request.headers.get("X-Hub-Signature")
    )
    secrets = [
        str(getattr(settings, "meta_app_secret", "") or ""),
        str(getattr(settings, "meta_ig_app_secret", "") or ""),
    ]
    if not verify_meta_signatures(
        app_secrets=secrets,
        body=body,
        signature_header_sha256=signature_sha256,
        signature_header_sha1=signature_sha1,
    ):
        app_secret = str(getattr(settings, "meta_app_secret", "") or "").strip()
        ig_secret = str(getattr(settings, "meta_ig_app_secret", "") or "").strip()
        verify_token = str(getattr(settings, "meta_verify_token", "") or "").strip()
        rejected = {
            "has_sha256": bool((signature_sha256 or "").strip()),
            "has_sha1": bool((signature_sha1 or "").strip()),
            "sha256_prefix": (signature_sha256 or "")[:7],
            "sha256_len": len((signature_sha256 or "").strip()),
            "secret_slots": sum(1 for item in secrets if (item or "").strip()),
            "app_secret_len": len(app_secret),
            "ig_secret_len": len(ig_secret),
            "ig_same_as_app": bool(ig_secret) and ig_secret == app_secret,
            "ig_looks_like_verify_token": bool(ig_secret) and ig_secret == verify_token,
            "ig_looks_like_igaa": ig_secret.startswith("IGAA"),
            "body_bytes": len(body),
            "header_keys": sorted(request.headers.keys()),
        }
        print("[meta.webhook.signature_rejected]", rejected)
        log_event("meta.webhook.signature_rejected", rejected)
        raise HTTPException(status_code=401, detail={"error": "invalid_meta_signature"})

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_json"}) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": "invalid_payload"})

    entries = payload.get("entry") if isinstance(payload.get("entry"), list) else []
    log_event(
        "meta.webhook.received",
        {
            "object": str(payload.get("object") or "")[:32],
            "entries": len(entries),
            "has_messaging": any(
                isinstance(entry, dict)
                and (
                    isinstance(entry.get("messaging"), list)
                    or isinstance(entry.get("changes"), list)
                )
                for entry in entries
            ),
            "has_standby": any(
                isinstance(entry, dict) and isinstance(entry.get("standby"), list)
                for entry in entries
            ),
            "payload_skeleton": payload_skeleton(payload),
            "bytes": len(body),
        },
    )

    messages = parse_meta_instagram_messaging(payload)
    queued: list[dict] = []
    for incoming in messages:
        created, inbox_id = enqueue_inbound(
            provider="meta",
            channel="instagram",
            message_id=incoming.message_id,
            conversation_key=incoming.conversation_id or incoming.sender_key,
            visitor_id=incoming.visitor_id,
            sender_key=incoming.sender_key,
            event_name="meta_messaging",
            payload={
                "normalized": incoming.model_dump(mode="json"),
                "raw": incoming.raw,
            },
        )
        queued.append({"created": created, "inbox_id": inbox_id})

    change_fields: list[str] = []
    entry_keys: list[list[str]] = []
    for entry in entries[:4]:
        if not isinstance(entry, dict):
            continue
        entry_keys.append(sorted(str(key) for key in entry.keys()))
        changes = entry.get("changes")
        if isinstance(changes, list):
            for change in changes[:6]:
                if isinstance(change, dict):
                    change_fields.append(str(change.get("field") or "")[:40])
    result_log = {
        "object": str(payload.get("object") or "")[:32],
        "entries": len(entries),
        "entry_keys": entry_keys,
        "change_fields": change_fields,
        "messaging_shapes": messaging_event_shapes(payload),
        "payload_skeleton": payload_skeleton(payload),
        "messages": len(messages),
        "queued": queued,
    }
    print("[meta.webhook.result]", result_log)
    log_event("meta.webhook.result", result_log)

    return JSONResponse(
        {
            "ok": True,
            "provider": "meta",
            "messages": len(messages),
            "queued": queued,
        }
    )
