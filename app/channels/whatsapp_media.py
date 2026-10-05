"""Native WhatsApp images: upload bytes, then send the returned media id.

This is an opt-in transport for the SAME number configured in Brevo. Brevo's
public text endpoint does not accept imageUrl. Never fall back to a text link.
"""
from __future__ import annotations

import asyncio
from io import BytesIO

import httpx
from PIL import Image

from app.channels.product_photos import safe_catalog_photo
from app.config import get_settings
from app.core.remote_media import download_trusted_media
from app.identity.repository import normalize_phone
from app.models import BrevoSendResult


def media_configured(settings) -> bool:
    return bool(getattr(settings, "meta_whatsapp_media_enabled", False)
                and getattr(settings, "meta_whatsapp_access_token", "")
                and getattr(settings, "meta_whatsapp_phone_number_id", ""))


async def send_image_files(incoming, urls: list[str]) -> BrevoSendResult:
    from app.evaluation.context import prohibit_side_effect
    prohibit_side_effect("channel_send")
    settings = get_settings()
    accepted = []
    status = None

    def result(error=None):
        return BrevoSendResult(ok=error is None, dry_run=False, status_code=status,
            error=error, provider_response={"route": "whatsapp_cloud_media",
                "images_requested": len(urls), "images_accepted": len(accepted),
                "message_ids": accepted})

    if not media_configured(settings):
        return result("whatsapp_media_not_configured")
    recipient = normalize_phone(incoming.sender_phone)
    expected_sender = normalize_phone(settings.brevo_sender_number)
    if incoming.channel != "whatsapp" or not recipient or not expected_sender:
        return result("whatsapp_media_recipient_or_sender_missing")
    if not urls or len(urls) > 10 or any(not safe_catalog_photo(url) for url in urls):
        return result("whatsapp_media_invalid_image")
    if settings.dry_run or settings.brevo_reply_mode == "dry_run":
        return BrevoSendResult(ok=True, dry_run=True)

    root = (f"https://graph.facebook.com/{settings.meta_whatsapp_graph_version}/"
            f"{settings.meta_whatsapp_phone_number_id}")
    headers = {"Authorization": f"Bearer {settings.meta_whatsapp_access_token}"}

    def body(response):
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("invalid_provider_response")
        return value

    sending = False
    try:
        async with asyncio.timeout(90), httpx.AsyncClient(timeout=20) as client:
            # Prevent an independently configured token/phone id from sending
            # customer messages under a different business number.
            account = await client.get(root, params={"fields": "display_phone_number"}, headers=headers)
            status = account.status_code
            account.raise_for_status()
            if normalize_phone(body(account).get("display_phone_number")) != expected_sender:
                return result("whatsapp_media_sender_mismatch")
            # Upload every file before sending any, so an invalid second image
            # cannot result in a partially delivered batch.
            media_ids = []
            for position, url in enumerate(urls, 1):
                data, _ = await download_trusted_media(url, kind="image", max_bytes=5_000_000,
                    timeout_seconds=15, allowed_suffixes=("tcdn.com.br",))
                with Image.open(BytesIO(data)) as image:
                    mime = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(image.format)
                    if not mime or image.width * image.height > 25_000_000:
                        return result("whatsapp_media_invalid_image")
                    image.verify()
                extension = "jpg" if mime == "image/jpeg" else "png"
                upload = await client.post(root + "/media", headers=headers,
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (f"produto-{position}.{extension}", data, mime)})
                status = upload.status_code
                upload.raise_for_status()
                media_id = body(upload).get("id")
                if not isinstance(media_id, str) or not media_id:
                    return result("whatsapp_media_invalid_upload_response")
                media_ids.append(media_id)
            for media_id in media_ids:
                sending = True
                sent = await client.post(root + "/messages", headers=headers, json={
                    "messaging_product": "whatsapp", "recipient_type": "individual",
                    "to": recipient, "type": "image", "image": {"id": media_id}})
                status = sent.status_code
                sent.raise_for_status()
                messages = body(sent).get("messages") or []
                if not isinstance(messages, list):
                    return result("whatsapp_media_delivery_uncertain")
                message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
                if not isinstance(message_id, str) or not message_id:
                    return result("whatsapp_media_delivery_uncertain")
                accepted.append(message_id)
                sending = False
    except httpx.HTTPStatusError:
        return result("whatsapp_media_partial_delivery" if accepted else "whatsapp_media_rejected")
    except (httpx.HTTPError, TimeoutError, ValueError, OSError, Image.DecompressionBombError):
        # An ambiguous send or an acknowledged partial batch must not be
        # blindly replayed: the outbox surfaces it for operator investigation.
        return result("whatsapp_media_delivery_uncertain" if sending else
                      "whatsapp_media_partial_delivery" if accepted else "whatsapp_media_upload_failed")
    return result()
