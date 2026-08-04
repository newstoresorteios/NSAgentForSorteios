from types import SimpleNamespace

import pytest

from app.image_product_id import (
    ImageProductIdentification,
    handle_image_product_search,
    identify_product_from_image,
    image_search_eligible,
    interpretation_from_identification,
)
from app.models import AgentResult, IncomingMessage
from app.webhook_parser import parse_brevo_conversations_payload


def _image_payload(*, with_caption: bool = False) -> dict:
    message = {
        "id": "msg-image-001",
        "type": "visitor",
        "createdAt": 1785700000000,
        "file": {
            "link": "https://example.com/watches/certina.jpg",
            "mimeType": "image/jpeg",
            "name": "certina.jpg",
            "type": "image",
        },
    }
    if with_caption:
        message["text"] = "tem esse?"
    return {
        "eventName": "conversationFragment",
        "conversationId": "conv-image-001",
        "messages": [message],
        "visitor": {
            "id": "visitor-wa",
            "source": "whatsapp",
            "attributes": {"WHATSAPP": "5521999999999"},
        },
    }


def test_parser_persists_image_url_for_whatsapp_photo():
    incoming = parse_brevo_conversations_payload(_image_payload())

    assert incoming.attachment_type == "image"
    assert incoming.input_modality == "image"
    assert incoming.image_url == "https://example.com/watches/certina.jpg"
    assert incoming.image_mime_type == "image/jpeg"
    assert "Imagem recebida" in incoming.text


def test_parser_keeps_caption_with_image():
    incoming = parse_brevo_conversations_payload(_image_payload(with_caption=True))

    assert incoming.input_modality == "text_with_image"
    assert incoming.image_url == "https://example.com/watches/certina.jpg"
    assert incoming.text == "tem esse?"


def test_image_search_eligible_requires_flag_and_url(monkeypatch):
    from app import image_product_id as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(agent_image_search_enabled=True),
    )
    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/a.jpg",
    )
    assert image_search_eligible(message) is True

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(agent_image_search_enabled=False),
    )
    assert image_search_eligible(message) is False


def test_interpretation_ignores_vision_color_as_reference():
    from app.image_product_id import (
        ImageProductIdentification,
        interpretation_from_identification,
    )

    identified = ImageProductIdentification(
        is_watch=True,
        brand="Christopher Ward",
        model="Sealander Automatic",
        reference="rosa claro (mostrador)",
        color=None,
        confidence=0.9,
    )
    interpretation = interpretation_from_identification(identified)
    assert interpretation.subject.reference is None
    assert interpretation.preferences.color
    assert "rosa" in interpretation.preferences.color.casefold()
    assert "Sealander" in (interpretation.subject.model or "")


def test_interpretation_from_identification_builds_find_subject():
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Certina",
        model="DS Super PH2000M Automático",
        reference="C050.607.44.011.02",
        color="Branco",
        confidence=0.91,
    )
    interpretation = interpretation_from_identification(identified)

    assert interpretation.domain == "commerce"
    assert interpretation.goal == "find"
    assert interpretation.ready_for_retrieval is True
    assert interpretation.subject.brand == "Certina"
    assert interpretation.subject.reference == "C050.607.44.011.02"
    assert "PH2000M" in (interpretation.subject.model or "")
    assert "Branco" in (interpretation.subject.model or "")


@pytest.mark.asyncio
async def test_identify_product_from_image_uses_vision_parse(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/watch.jpg",
        image_mime_type="image/jpeg",
    )
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Christopher Ward",
        model="C63 Sealander",
        color="Rosa",
        confidence=0.88,
    )

    async def fake_download(url, *, max_bytes=None):
        return b"fake-image-bytes", "image/jpeg"

    async def fake_parse(*, model, text_format, messages, temperature=None, call_type="structured", **kwargs):
        assert call_type == "image_product_identify"
        assert text_format is ImageProductIdentification
        assert any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for part in messages
            for block in (
                part.get("content") if isinstance(part.get("content"), list) else []
            )
        )
        return SimpleNamespace(parsed=identified, api_mode="chat_completions")

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        openai_api_key="sk-test",
        openai_model="gpt-4.1-mini",
        agent_image_search_model="",
        agent_image_download_max_bytes=8_000_000,
    ))
    monkeypatch.setattr(module, "download_image_file", fake_download)
    monkeypatch.setattr("app.openai_gateway.parse_structured_output", fake_parse)

    result = await identify_product_from_image(message)
    assert result.brand == "Christopher Ward"
    assert result.confidence == 0.88


@pytest.mark.asyncio
async def test_handle_image_product_search_retrieves_catalog(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/watch.jpg",
    )
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Certina",
        model="DS Super PH2000M Automático Branco Titânio",
        confidence=0.9,
    )
    tray_result = AgentResult(
        reply_text="Encontrei o Certina DS Super PH2000M.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "9001",
                    "name": "Relógio Certina DS Super PH2000M Automático Branco Titânio",
                    "brand": "Certina",
                }
            ]
        },
        response_metadata={"used_tray": True},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        assert interpretation.subject.brand == "Certina"
        assert "PH2000M" in (interpretation.subject.model or "")
        return tray_result

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.response_metadata.get("image_search") is True
    assert result.response_metadata.get("clear_active_product") is True
    assert result.response_metadata.get("product_resolution_state") == (
        "plausible_matches"
    )
    assert "Certina" in result.reply_text
    assert "É esse que você procura?" in result.reply_text
    assert result.commercial_data["products"][0]["id"] == "9001"


@pytest.mark.asyncio
async def test_handle_image_ambiguous_siblings_does_not_activate(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="qual o preço desse?",
        input_modality="text_with_image",
        attachment_type="image",
        image_url="https://example.com/beaubleu.jpg",
    )
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Beaubleu",
        model="Ecce",
        color="branco prata pulseira bege",
        confidence=0.9,
    )
    tray_result = AgentResult(
        reply_text="Encontrei opções.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "15522",
                    "name": "Relógio Beaubleu Ecce Smalt Automático Prata 39 mm",
                    "brand": "Beaubleu",
                },
                {
                    "id": "15860",
                    "name": "Relógio Beaubleu Ecce Lys Automático Branco",
                    "brand": "Beaubleu",
                },
            ],
            "match_status": "ambiguous",
        },
        response_metadata={
            "used_tray": True,
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
        },
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        from app.product_retrieval import catalog_match_tokens, preference_color_tokens

        assert "pulseira" not in catalog_match_tokens(interpretation)
        assert "bege" not in catalog_match_tokens(interpretation)
        assert "prata" not in catalog_match_tokens(interpretation)
        assert preference_color_tokens(interpretation) == ("branco",)
        return tray_result

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert "É algum desses?" in result.reply_text
    assert result.commercial_data.get("match_status") == "ambiguous"
    assert result.response_metadata.get("clear_active_product") is True
    assert "active_product" not in result.response_metadata


@pytest.mark.asyncio
async def test_handle_image_product_search_does_not_ask_for_sku(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="qual o preço desse?",
        input_modality="text_with_image",
        attachment_type="image",
        image_url="https://example.com/sealander.jpg",
    )
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Christopher Ward",
        model="Sealander Automatic",
        color="rosa claro",
        confidence=0.92,
    )
    tray_result = AgentResult(
        reply_text="Não encontrei esse produto no catálogo agora.",
        intent="commerce",
        safety_reason="product_not_found",
        response_metadata={"used_tray": True},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        return tray_result

    async def fake_visual(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=False,
        database_url="",
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert "referência" not in result.reply_text.casefold()
    assert "opções mais próximas" in result.reply_text.casefold()
    assert "Christopher Ward" in result.reply_text


@pytest.mark.asyncio
async def test_handle_image_product_search_asks_when_confidence_low(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/blur.jpg",
    )

    async def fake_identify(msg):
        return ImageProductIdentification(
            is_watch=True,
            brand="Certina",
            model=None,
            confidence=0.2,
        )

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=False,
        database_url="",
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.safety_reason == "image_identify_low_confidence"
    assert "não consegui" in result.reply_text.casefold() or "confirma" in result.reply_text.casefold()


def test_parser_detects_image_from_url_extension_without_type():
    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conv-image-002",
        "messages": [
            {
                "id": "msg-image-002",
                "type": "visitor",
                "text": "qual o preço do relogio da foto?",
                "createdAt": 1785700000000,
                "file": {
                    "link": "https://cdn.example.com/watch.jpg",
                    "name": "attachment",
                    "type": "file",
                },
            }
        ],
        "visitor": {
            "id": "visitor-wa",
            "source": "whatsapp",
            "attributes": {"WHATSAPP": "5521999999999"},
        },
    }
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.attachment_type == "image"
    assert incoming.image_url == "https://cdn.example.com/watch.jpg"
    assert incoming.input_modality == "text_with_image"
