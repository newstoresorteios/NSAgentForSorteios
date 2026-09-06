from types import SimpleNamespace

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage, SalesInterpretation


def _settings():
    return SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini")


def _interpretation(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "buy",
        "subject": {"product_type": "produto"},
        "preferences": {},
        "information_needed": [],
        "references_previous_context": True,
        "needs_clarification": False,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.asyncio
async def test_purchase_close_does_not_run_leftover_tray_loop(monkeypatch):
    import app.sales_agent as sales_agent
    from app.sales.catalog_retrieve import retrieve_catalog_or_clarify

    async def boom(*_args, **_kwargs):
        raise AssertionError("leftover must not call handle_commerce_message")

    monkeypatch.setattr(sales_agent, "handle_commerce_message", boom)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)

    result = await retrieve_catalog_or_clarify(
        message=IncomingMessage(text="quero fechar"),
        facts={},
        customer_context={},
        interpretation=_interpretation(),
        plan={"intent": "coupon", "query": "desconto", "goal": "buy"},
        state=CommerceConversationState(
            last_presented_products=[
                {"position": 1, "product_id": "P1", "name": "Seiko 5"},
            ]
        ),
        recent_turns=None,
        resolved_product=None,
    )

    assert result is not None
    assert result.safety_reason == "purchase_close_hold"
    assert "qual opção" in result.reply_text.casefold()


@pytest.mark.asyncio
async def test_image_request_skips_openai_when_official_url_in_facts(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product"
        return {
            "id": arguments["product_id"],
            "name": "Seiko 5 Sports",
            "image_url": "https://cdn.tray.example/seiko.jpg",
        }

    async def boom_responder(*_args, **_kwargs):
        raise AssertionError("official image URL must not go through OpenAI")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", boom_responder)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="manda a foto"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            image_request=True,
            reference_type="current_product",
        ),
        commerce_state=CommerceConversationState(
            active_product={
                "product_id": "P1",
                "name": "Seiko 5 Sports",
                "reference": "SRPD53",
            }
        ),
    )

    assert result is not None
    assert result.response_metadata.get("used_openai_responder") is False
    assert "https://cdn.tray.example/seiko.jpg" in result.reply_text


@pytest.mark.asyncio
async def test_dead_product_link_does_not_offer_human_handoff(monkeypatch):
    import app.sales_agent as sales_agent

    async def execute(tool, arguments):
        assert tool == "get_product_link"
        return {
            "product_id": arguments["product_id"],
            "product_name": "Seiko 5 Sports",
            "product_url": None,
            "product_url_dead": True,
            "reference": "SRPD53",
            "current_price": "1890.00",
        }

    async def boom_responder(*_args, **_kwargs):
        raise AssertionError("dead product link must not go through OpenAI")

    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", boom_responder)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="me passa o link"),
        {},
        {},
        _interpretation(
            goal="inspect",
            purchase_action=None,
            product_action="get_product_link",
            reference_type="current_product",
        ),
        commerce_state=CommerceConversationState(
            active_product={
                "product_id": "P1",
                "name": "Seiko 5 Sports",
                "reference": "SRPD53",
            }
        ),
    )

    assert result is not None
    text = result.reply_text.casefold()
    assert "não consegui o link agora" in text
    assert "srpd53" in text
    assert "1890" in result.reply_text
    assert "atendimento" not in text
    assert "consultor" not in text
    assert "joão" not in text


@pytest.mark.asyncio
async def test_pending_confirm_with_visible_cart_url_uses_pay_link(monkeypatch):
    import app.sales_agent as sales_agent

    async def boom_execute(*_args, **_kwargs):
        raise AssertionError("visible cart must not reopen create_cart or channel")

    async def boom_responder(*_args, **_kwargs):
        raise AssertionError("visible cart pay-link must not go through OpenAI")

    monkeypatch.setattr(sales_agent, "execute_tool", boom_execute)
    monkeypatch.setattr(sales_agent, "get_settings", _settings)
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", boom_responder)

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="confirmação semântica"),
        {},
        {},
        _interpretation(confirmation="confirm", goal="buy"),
        commerce_state=CommerceConversationState(
            active_domain="commerce",
            cart_session_id="S1",
            cart_url="https://loja.example/checkout/S1",
            pending_action="choose_checkout_channel",
            purchase_stage="cart_created",
            cart_items=[{"product_id": "P1", "quantity": 1}],
        ),
    )

    assert result is not None
    text = result.reply_text.casefold()
    assert "https://loja.example/checkout/s1" in text
    assert "paga" in text
    assert "joão" not in text
    assert "consultor" not in text
    assert result.response_metadata.get("used_openai_responder") is False
    assert result.response_metadata.get("clear_pending_action") is True
