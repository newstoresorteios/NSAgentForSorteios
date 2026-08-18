"""Meta Instagram Messaging webhook adapter (FASE 3).

Requires META_* env vars. Signature verification uses X-Hub-Signature-256.
Media URLs are fetched via Graph when not present on the attachment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.config import get_settings
from app.models import AgentResult, IncomingMessage
from app.observability import log_event


def meta_webhook_enabled() -> bool:
    settings = get_settings()
    return bool(getattr(settings, "meta_webhook_enabled", False))


def _normalize_meta_secret(value: str | None) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def verify_meta_signature(*, app_secret: str, body: bytes, signature_header: str | None) -> bool:
    return verify_meta_signatures(
        app_secrets=[app_secret],
        body=body,
        signature_header_sha256=signature_header,
        signature_header_sha1=None,
    )


def verify_meta_signatures(
    *,
    app_secrets: list[str],
    body: bytes,
    signature_header_sha256: str | None,
    signature_header_sha1: str | None = None,
) -> bool:
    """HMAC-verify Meta webhook signatures.

    Instagram Login often signs with the Instagram app secret, which can differ
    from the Facebook app secret. Also accept legacy X-Hub-Signature (sha1).
    """
    secrets = [_normalize_meta_secret(secret) for secret in app_secrets]
    secrets = [secret for secret in secrets if secret]
    if not secrets:
        return False

    sha256_header = (signature_header_sha256 or "").strip()
    sha1_header = (signature_header_sha1 or "").strip()
    provided_sha256 = (
        sha256_header.split("=", 1)[1].strip()
        if sha256_header.startswith("sha256=")
        else sha256_header
    )
    provided_sha1 = (
        sha1_header.split("=", 1)[1].strip()
        if sha1_header.startswith("sha1=")
        else sha1_header
    )

    for secret in secrets:
        key = secret.encode("utf-8")
        if provided_sha256:
            digest = hmac.new(key, body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(digest, provided_sha256):
                return True
        if provided_sha1:
            digest = hmac.new(key, body, hashlib.sha1).hexdigest()
            if hmac.compare_digest(digest, provided_sha1):
                return True
    return False


def handle_meta_verify_challenge(
    *,
    mode: str | None,
    verify_token: str | None,
    challenge: str | None,
    expected_token: str,
) -> str | None:
    if mode != "subscribe":
        return None
    if not expected_token or verify_token != expected_token:
        return None
    return challenge or ""


def _instagram_messaging_events(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect messaging events from Messenger-style and Instagram changes payloads."""
    events: list[dict[str, Any]] = []
    for key in ("messaging", "standby"):
        block = entry.get(key)
        if isinstance(block, list):
            events.extend(item for item in block if isinstance(item, dict))

    changes = entry.get("changes")
    if not isinstance(changes, list):
        return events
    for change in changes:
        if not isinstance(change, dict):
            continue
        field = str(change.get("field") or "").strip().lower()
        if field and field not in {"messages", "messaging", "standby"}:
            continue
        value = change.get("value")
        if isinstance(value, list):
            events.extend(item for item in value if isinstance(item, dict))
            continue
        if not isinstance(value, dict):
            continue
        nested = value.get("messaging") or value.get("standby")
        if isinstance(nested, list):
            events.extend(item for item in nested if isinstance(item, dict))
        elif "sender" in value or "message" in value:
            events.append(value)
    return events


def parse_meta_instagram_messaging(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Normalize Meta Instagram messaging webhooks into IncomingMessage list."""
    settings = get_settings()
    account_id = str(getattr(settings, "meta_ig_business_account_id", "") or "").strip()
    messages: list[IncomingMessage] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        log_event(
            "meta.instagram.parsed",
            {"messages": 0, "entries": 0, "reason": "no_entry"},
        )
        return messages

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        messaging = _instagram_messaging_events(entry)
        for event in messaging:
            if not isinstance(event, dict):
                continue
            sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
            recipient = (
                event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
            )
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if not message or message.get("is_echo"):
                continue
            sender_id = str(sender.get("id") or "").strip()
            recipient_id = str(recipient.get("id") or "").strip() or account_id
            message_id = str(message.get("mid") or "").strip() or None
            text = str(message.get("text") or "").strip()
            image_url = None
            attachment_type = None
            input_modality = "text"
            attachments = message.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    atype = str(attachment.get("type") or "").lower()
                    payload_obj = (
                        attachment.get("payload")
                        if isinstance(attachment.get("payload"), dict)
                        else {}
                    )
                    url = str(payload_obj.get("url") or "").strip() or None
                    if atype in {"image", "story_mention"} and url:
                        image_url = url
                        attachment_type = "image"
                        input_modality = "text_with_image" if text else "image"
                        break
                    if atype == "video" and url:
                        # Treated as media for Story/video pipeline later.
                        image_url = url
                        attachment_type = "video"
                        input_modality = "text_with_image" if text else "image"
                        break

            story_ctx = None
            reply_to = message.get("reply_to")
            if isinstance(reply_to, dict) and isinstance(reply_to.get("story"), dict):
                from pydantic import SecretStr

                from app.instagram_story_models import InstagramStoryContext
                from app.instagram_story_parser import safe_media_reference

                story = reply_to["story"]
                story_url = str(story.get("url") or "").strip() or None
                story_ctx = InstagramStoryContext(
                    replied_to_story=True,
                    mentioned_in_story=False,
                    story_media_id=str(story.get("id") or "").strip() or None,
                    story_media_url_private=(
                        SecretStr(story_url) if story_url else None
                    ),
                    story_media_log_reference=(
                        safe_media_reference(story_url) if story_url else None
                    ),
                    media_type="image",
                    provider="meta",
                    instagram_account_id=recipient_id or "",
                    raw_reference={"reply_to": reply_to},
                )
                if story_url and not image_url:
                    image_url = story_url
                    attachment_type = attachment_type or "image"
                    input_modality = "text_with_image" if text else "image"

            if not text and not image_url:
                continue

            if not text and image_url:
                text = "[Imagem recebida via Instagram]"

            messages.append(
                IncomingMessage(
                    provider="meta",
                    channel="instagram",
                    event_type="meta_messaging",
                    message_id=message_id,
                    conversation_id=f"ig:{sender_id}" if sender_id else None,
                    visitor_id=sender_id or None,
                    sender_key=f"instagram:{sender_id}" if sender_id else None,
                    sender_external_id=sender_id or None,
                    text=text,
                    image_url=image_url,
                    attachment_type=attachment_type,
                    input_modality=input_modality,
                    instagram_story=story_ctx,
                    raw={"meta_event": event, "entry_id": entry.get("id")},
                )
            )

    log_event(
        "meta.instagram.parsed",
        {
            "messages": len(messages),
            "entries": len(entries),
            "object": str(payload.get("object") or "")[:32],
        },
    )
    return messages


async def send_meta_instagram_reply(
    incoming: IncomingMessage,
    result: AgentResult,
) -> dict[str, Any]:
    """Send text reply via Instagram Messaging Graph API."""
    import httpx

    settings = get_settings()
    token = str(getattr(settings, "meta_page_access_token", "") or "").strip()
    if not token:
        return {"ok": False, "error": "meta_page_access_token_missing"}
    recipient_id = (
        incoming.sender_external_id
        or (incoming.sender_key or "").split(":", 1)[-1]
        or incoming.visitor_id
    )
    if not recipient_id:
        return {"ok": False, "error": "meta_recipient_missing"}

    ig_account_id = str(
        getattr(settings, "meta_ig_business_account_id", "") or ""
    ).strip()
    text = (result.reply_text or "")[:2000]
    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }

    # Instagram Login tokens (IGAA...) use graph.instagram.com /me first.
    # The dashboard IG Business ID (17841…) is not always the send path ID.
    endpoints: list[str] = []
    if token.startswith("IGAA"):
        endpoints.append("https://graph.instagram.com/v21.0/me/messages")
        if ig_account_id:
            endpoints.append(
                f"https://graph.instagram.com/v21.0/{ig_account_id}/messages"
            )
    elif ig_account_id:
        endpoints.append(
            f"https://graph.instagram.com/v21.0/{ig_account_id}/messages"
        )
        endpoints.append("https://graph.instagram.com/v21.0/me/messages")
    endpoints.append("https://graph.facebook.com/v21.0/me/messages")

    last_body: dict[str, Any] = {}
    last_status = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for url in endpoints:
            resp = await client.post(
                url,
                params={"access_token": token},
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            last_status = resp.status_code
            try:
                last_body = resp.json()
            except Exception:
                last_body = {"raw": (resp.text or "")[:200]}
            if resp.status_code < 300:
                log_event(
                    "meta.instagram.send",
                    {
                        "ok": True,
                        "status_code": resp.status_code,
                        "endpoint_host": url.split("/")[2],
                        "recipient_present": True,
                    },
                )
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "provider_response": last_body,
                    "endpoint": url.split("/v21.0/", 1)[-1],
                }

    log_event(
        "meta.instagram.send",
        {
            "ok": False,
            "status_code": last_status,
            "recipient_present": True,
            "error_code": (last_body or {}).get("error", {}).get("code")
            if isinstance(last_body.get("error"), dict)
            else None,
        },
    )
    return {
        "ok": False,
        "status_code": last_status,
        "provider_response": last_body,
        "error": "meta_send_failed",
    }
