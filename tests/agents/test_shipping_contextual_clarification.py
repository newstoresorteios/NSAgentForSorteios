from unittest.mock import AsyncMock

import pytest

from app.agents.commerce_order import (
    _shipping_destination_clarification,
    try_commerce_checkout_routes,
)
from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage, SalesInterpretation


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Which country and ZIP/postal code should I check for shipping?",
            "Which country",
        ),
        (
            "¿Qué producto te interesa y a qué ciudad o código postal de Ghana sería la entrega?",
            "Ghana",
        ),
    ],
)
def test_international_shipping_question_preserves_context_and_language(
    question,
    expected,
):
    interpretation = SalesInterpretation(
        goal="buy",
        domain="commerce",
        references_previous_context=False,
        confidence=0.9,
        shipping_action="quote",
        shipping_zipcode=None,
        needs_clarification=True,
        clarification_question=question,
    )

    result = _shipping_destination_clarification(interpretation)

    assert result is not None
    assert expected in result.reply_text
    assert "RS" not in result.reply_text
    assert "Porto Alegre" not in result.reply_text
    assert result.safety_reason == "shipping_destination_required"
    assert result.response_metadata["used_tray"] is False
    assert result.response_metadata["preserve_customer_language"] is True


def test_shipping_with_brazilian_zipcode_continues_to_live_quote():
    interpretation = SalesInterpretation(
        goal="buy",
        domain="commerce",
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
        shipping_action="quote",
        shipping_zipcode="19900000",
        clarification_question="Qual é o CEP?",
    )

    assert _shipping_destination_clarification(interpretation) is None


@pytest.mark.asyncio
async def test_worldwide_delivery_incident_bypasses_regional_fallback(monkeypatch):
    import app.sales_agent as sales

    quote = AsyncMock(side_effect=AssertionError("must ask destination before quote"))

    async def passthrough(**kwargs):
        return kwargs["result"]

    monkeypatch.setattr(sales, "quote_shipping", quote)
    monkeypatch.setattr(sales, "_respond_to_commerce_service", passthrough)
    monkeypatch.setattr(sales, "evolve_commerce_state", lambda state, _result: state)
    interpretation = SalesInterpretation(
        goal="buy",
        domain="commerce",
        references_previous_context=False,
        confidence=0.9,
        shipping_action="quote",
        shipping_zipcode=None,
        needs_clarification=True,
        clarification_question=(
            "Which country and ZIP/postal code should I check for shipping?"
        ),
    )

    result = await try_commerce_checkout_routes(
        message=IncomingMessage(
            text="You do worldwide deliveries? How much is it?",
            channel="instagram",
        ),
        interpretation=interpretation,
        plan={"intent": "commerce", "goal": "buy"},
        state=CommerceConversationState(),
    )

    assert result.reply_text.startswith("Which country")
    assert result.response_metadata["response_source"] == (
        "shipping_contextual_clarification"
    )
    quote.assert_not_awaited()
