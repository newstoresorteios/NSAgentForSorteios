"""Download inbound photos and run structured Vision identification."""

from __future__ import annotations

import base64
from typing import Any

import httpx
from app.config import get_settings
from app.models import IncomingMessage
from app.catalog.vision.prompt import (
    IMAGE_IDENTIFY_INSTRUCTIONS,
    ImageProductIdentification,
)


def image_search_eligible(message: IncomingMessage) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "agent_image_search_enabled", True)):
        return False
    if not (message.image_url or "").strip():
        return False
    attachment = (message.attachment_type or "").lower()
    modality = (message.input_modality or "").lower()
    if attachment == "image":
        return True
    return modality in {"image", "text_with_image"}


async def download_image_file(
    url: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    settings = get_settings()
    limit = max_bytes or int(
        getattr(settings, "agent_image_download_max_bytes", 8_000_000)
    )
    headers = {
        "User-Agent": "NewStoreAgent/1.0",
        "Accept": "image/*,*/*",
    }
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
    if len(content) > limit:
        raise ValueError("image_too_large")
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return content, content_type


def _vision_model() -> str:
    settings = get_settings()
    configured = str(getattr(settings, "agent_image_search_model", "") or "").strip()
    return configured or settings.openai_model


def _caption_hint(message: IncomingMessage) -> str | None:
    text = (message.text or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered.startswith("[imagem recebida") or lowered.startswith("[sticker recebido"):
        return None
    return text


async def identify_product_from_image(
    message: IncomingMessage,
) -> ImageProductIdentification:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")
    image_url = (message.image_url or "").strip()
    if not image_url:
        raise ValueError("image_url_missing")

    image_bytes, content_type = await download_image_file(image_url)
    if message.image_mime_type and str(message.image_mime_type).startswith("image/"):
        content_type = str(message.image_mime_type).split(";")[0].strip()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{encoded}"
    caption = _caption_hint(message)
    user_text = "Identifique o relógio nesta foto para busca no catálogo da loja."
    if caption:
        user_text += f"\nLegenda do cliente: {caption}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": IMAGE_IDENTIFY_INSTRUCTIONS},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                },
            ],
        },
    ]
    model = _vision_model()
    print("[sales.image.identify.request]", {
        "model": model,
        "has_caption": bool(caption),
        "image_bytes": len(image_bytes),
        "content_type": content_type,
    })
    from app.llm.openai_gateway import parse_structured_output

    parse_result = await parse_structured_output(
        model=model,
        text_format=ImageProductIdentification,
        messages=messages,
        temperature=0,
        call_type="image_product_identify",
    )
    identified = parse_result.parsed
    if not isinstance(identified, ImageProductIdentification):
        raise ValueError("image_identify_schema_missing")
    print("[sales.image.identify]", {
        "is_watch": identified.is_watch,
        "has_brand": bool(identified.brand),
        "has_model": bool(identified.model),
        "has_reference": bool(identified.reference),
        "confidence": identified.confidence,
    })
    return identified
