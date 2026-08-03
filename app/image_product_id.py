from __future__ import annotations

import base64
from typing import Any

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from .config import get_settings
from .models import AgentResult, IncomingMessage, SalesInterpretation
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


IMAGE_IDENTIFY_INSTRUCTIONS = """\
Você identifica relógios em fotos enviadas por clientes da NewStore (loja de relógios).

Extraia apenas o que estiver legível ou claramente visível na imagem:
- marca (brand)
- modelo / linha (model), incluindo códigos no mostrador (ex.: PH2000M, Sealander, C63)
- referência comercial se aparecer (ex.: C050.607.44.011.02, C63-36ADA4-S00P0-B0)
- cor dominante do mostrador/caixa quando clara

Regras:
- is_watch=false se a imagem não for um relógio de pulso.
- Não invente referência. Se não ler a ref, deixe reference=null.
- confidence entre 0 e 1 conforme legibilidade.
- Preferir nomes comerciais usados em e-commerce BR (ex.: "DS Super PH2000M Automático Branco Titânio").
- Se houver legenda do cliente, use-a só como dica complementar — a imagem manda.
"""


class ImageProductIdentification(BaseModel):
    is_watch: bool = True
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    color: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str | None = None


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
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await execute_openai_call(
        call_type="image_product_identify",
        model=model,
        messages=messages,
        operation=lambda: client.chat.completions.parse(
            model=model,
            messages=messages,
            temperature=0,
            response_format=ImageProductIdentification,
        ),
    )
    parsed_message = response.choices[0].message if response.choices else None
    if parsed_message is None or getattr(parsed_message, "refusal", None):
        raise ValueError("image_identify_refusal_or_empty")
    identified = getattr(parsed_message, "parsed", None)
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


def interpretation_from_identification(
    identified: ImageProductIdentification,
) -> SalesInterpretation:
    model_parts = [
        part.strip()
        for part in (identified.model, identified.color)
        if part and str(part).strip()
    ]
    # Avoid duplicating color if already in model string.
    model = identified.model
    if identified.color and identified.model:
        if identified.color.casefold() not in identified.model.casefold():
            model = f"{identified.model} {identified.color}".strip()
    elif model_parts and not identified.model:
        model = " ".join(model_parts)

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "relógio",
            "brand": (identified.brand or "").strip() or None,
            "model": (model or "").strip() or None,
            "reference": (identified.reference or "").strip() or None,
        },
        preferences={
            "color": (identified.color or "").strip() or None,
        },
        information_needed=["catalog"],
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        stop_clarification=True,
        needs_clarification=False,
        clarification_question=None,
        confidence=float(identified.confidence or 0.0),
        active_topic="product_search",
        purchase_stage="discovery",
    )
    interpretation._source = "image_vision"
    return interpretation


def _clarification_result(
    *,
    reason: str,
    identified: ImageProductIdentification | None = None,
) -> AgentResult:
    if identified and not identified.is_watch:
        text = (
            "Recebi a imagem, mas não parece ser a foto de um relógio. "
            "Pode enviar a foto do relógio ou me dizer a marca e o modelo?"
        )
    elif identified and (identified.brand or identified.model):
        hint = " ".join(
            part
            for part in (identified.brand, identified.model)
            if part
        ).strip()
        text = (
            f"Não consegui confirmar a referência com segurança pela foto"
            f"{f' ({hint})' if hint else ''}. "
            "Me confirma a marca e o modelo, ou envia uma foto mais nítida do mostrador?"
        )
    else:
        text = (
            "Recebi a imagem, mas não consegui ler marca/modelo com segurança. "
            "Pode me dizer a marca e o modelo, ou enviar uma foto mais nítida?"
        )
    return AgentResult(
        reply_text=text,
        intent="commerce",
        handoff_required=False,
        safety_reason=reason,
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


def _visual_candidates_result(
    products: list[dict[str, Any]],
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult:
    from .commerce_router import _product_lines

    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(products), start=1)
    ]
    reply = (
        "Pela foto, estes parecem os mais próximos no catálogo:\n"
        + "\n".join(numbered_lines)
        + "\n\nÉ algum desses?"
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="visual_nearest_neighbor",
        commercial_data={"products": products},
        response_metadata={
            "domain": "commerce",
            "image_search": True,
            "visual_search": True,
            "visual_trigger": trigger,
            "image_identify": (
                identified.model_dump(mode="json") if identified is not None else None
            ),
        },
    )


async def _try_visual_fallback(
    message: IncomingMessage,
    *,
    identified: ImageProductIdentification | None,
    trigger: str,
) -> AgentResult | None:
    settings = get_settings()
    if not bool(getattr(settings, "agent_visual_search_enabled", True)):
        return None
    if not str(getattr(settings, "database_url", "") or "").strip():
        return None

    from .product_image_index import (
        caption_from_identification,
        visual_search_from_caption,
        visual_search_from_image_url,
    )

    products: list[dict[str, Any]] = []
    try:
        caption = ""
        if identified is not None:
            caption = caption_from_identification(identified)
        if caption:
            products = await visual_search_from_caption(caption)
        if not products and (message.image_url or "").strip():
            products = await visual_search_from_image_url(str(message.image_url).strip())
    except (
        APIError,
        LLMCallBudgetExceeded,
        httpx.HTTPError,
        ValueError,
        RuntimeError,
    ) as exc:
        print("[sales.image.visual.fallback.error]", {
            "trigger": trigger,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        return None

    if not products:
        return None

    top_k = int(getattr(settings, "agent_visual_top_k", 3) or 3)
    selected = products[: max(1, top_k)]
    print("[sales.image.visual.fallback]", {
        "trigger": trigger,
        "match_count": len(selected),
    })
    return _visual_candidates_result(
        selected,
        identified=identified,
        trigger=trigger,
    )


async def handle_image_product_search(
    message: IncomingMessage,
) -> AgentResult | None:
    """Identify a watch from an inbound image and search the Tray catalog."""
    if not image_search_eligible(message):
        return None

    settings = get_settings()
    min_confidence = float(
        getattr(settings, "agent_image_search_min_confidence", 0.55)
    )
    try:
        identified = await identify_product_from_image(message)
    except (APIError, LLMCallBudgetExceeded, httpx.HTTPError, ValueError, RuntimeError) as exc:
        print("[sales.image.identify.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        visual = await _try_visual_fallback(
            message,
            identified=None,
            trigger="image_identify_failed",
        )
        if visual is not None:
            return visual
        return AgentResult(
            reply_text=(
                "Recebi a imagem, mas não consegui analisar agora. "
                "Pode me dizer a marca e o modelo do relógio?"
            ),
            intent="commerce",
            handoff_required=False,
            safety_reason="image_identify_failed",
            response_metadata={"domain": "commerce", "image_search": True},
        )

    if not identified.is_watch:
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    low_confidence = (
        float(identified.confidence or 0.0) < min_confidence
        or not any((identified.brand, identified.model, identified.reference))
    )
    if low_confidence:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_identify_low_confidence",
        )
        if visual is not None:
            return visual
        return _clarification_result(
            reason="image_identify_low_confidence",
            identified=identified,
        )

    interpretation = interpretation_from_identification(identified)
    print("[sales.image.retrieval]", {
        "brand": interpretation.subject.brand,
        "model": interpretation.subject.model,
        "reference": interpretation.subject.reference,
        "confidence": identified.confidence,
    })

    from .sales_agent import (
        _execute_compiled_product_retrieval,
        _mark_sales_result,
    )

    tray_result = await _execute_compiled_product_retrieval(interpretation)
    if tray_result is None:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger="image_retrieval_empty",
        )
        if visual is not None:
            return visual
        return _clarification_result(
            reason="image_retrieval_empty",
            identified=identified,
        )

    label = " ".join(
        part
        for part in (
            interpretation.subject.brand,
            interpretation.subject.model or interpretation.subject.reference,
        )
        if part
    ).strip()
    if tray_result.safety_reason in {
        "product_not_found",
        "exact_product_ambiguous_brand",
    }:
        visual = await _try_visual_fallback(
            message,
            identified=identified,
            trigger=str(tray_result.safety_reason),
        )
        if visual is not None:
            return visual
        brand = interpretation.subject.brand or "dessa marca"
        tray_result.reply_text = (
            f"Pela foto, identifiquei {label or 'esse modelo'}, "
            f"mas não confirmei a referência exata no catálogo agora. "
            f"Quer que eu mostre opções {brand}?"
            if tray_result.safety_reason == "exact_product_ambiguous_brand"
            or interpretation.subject.brand
            else (
                f"Pela foto, identifiquei {label or 'esse modelo'}, "
                "mas não encontrei no catálogo agora. "
                "Pode confirmar a referência ou a marca?"
            )
        )

    tray_result.response_metadata.update({
        "image_search": True,
        "image_identify": identified.model_dump(mode="json"),
        "domain": "commerce",
        "used_tray": True,
    })
    # Skip OpenAI responder here: Vision already spent the critical latency budget.
    return _mark_sales_result(
        tray_result,
        interpretation=interpretation,
        goal="find",
        response_source="image_vision",
        used_openai_responder=False,
        used_tray=True,
        fallback_reason=tray_result.safety_reason,
    )
