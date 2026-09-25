from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage
from app.sales.service_intent_gate import service_intent_clarification


@pytest.mark.parametrize("text", [
    "Alguma notícia sobre a pulseira ?", "Tem novidade sobre o relógio?",
    "Algum retorno", "Na verdade é sobre o Frederique que veio com pulseira de metal",
])
def test_after_sales_cannot_turn_into_catalog(text):
    interpretation = SimpleNamespace(goal="after_sales", purchase_stage="after_sales")
    result = service_intent_clarification(text, interpretation, CommerceConversationState())
    assert result.safety_reason == "service_intent_clarification"
    assert "comprar (1" not in result.reply_text
    assert "R$" not in result.reply_text


@pytest.mark.parametrize("text", [
    "Quero uma pulseira", "Quero comprar uma pulseira", "Procuro um Seiko",
    "Até 2500", "Azul", "Automático", "Qual o preço?", "Quero o 1",
])
def test_existing_shopping_qualification_is_untouched(text):
    interpretation = SimpleNamespace(goal="discover", purchase_stage="discovery")
    assert service_intent_clarification(text, interpretation, CommerceConversationState()) is None


def test_new_purchase_overrides_stale_after_sales_classification():
    interpretation = SimpleNamespace(goal="after_sales", purchase_stage="after_sales")
    assert service_intent_clarification("Quero comprar outra pulseira", interpretation,
                                       CommerceConversationState()) is None


def test_known_order_keeps_existing_order_path():
    interpretation = SimpleNamespace(goal="after_sales", purchase_stage="after_sales")
    assert service_intent_clarification("Alguma notícia?", interpretation,
                                       CommerceConversationState(order_id="123")) is None


def test_explicit_human_request_is_not_replaced_by_qualification():
    interpretation = SimpleNamespace(goal="after_sales", purchase_stage="after_sales")
    assert service_intent_clarification("Terei que falar com atendente físico.", interpretation,
                                       CommerceConversationState()) is None


def test_published_handoff_policy_is_preserved():
    interpretation = SimpleNamespace(goal="after_sales", purchase_stage="after_sales",
                                     resolved_answer_strategy=lambda: "handoff")
    assert service_intent_clarification("Alguma notícia sobre a pulseira?", interpretation,
                                       CommerceConversationState()) is None


@pytest.mark.asyncio
async def test_real_incident_cannot_reach_recovery_or_catalog_even_if_interpreter_says_inspect(monkeypatch):
    from app.agents import commerce
    from app.sales import conversation_preflight
    interpretation = SimpleNamespace(goal="inspect", purchase_stage="discovery")
    sales = Mock()
    sales._hydrate_sales_interpretation.return_value = interpretation
    monkeypatch.setattr(commerce, "_sales", lambda: sales)
    monkeypatch.setattr(conversation_preflight, "preflight_reply", lambda _: None)
    monkeypatch.setattr(conversation_preflight, "normalize_identity", lambda item: item)
    result = await commerce.handle_sales_message_inner(
        IncomingMessage(text="Alguma notícia sobre a pulseira ?"), {}, {},
        commerce_state=CommerceConversationState(active_product={
            "product_id": "old", "name": "Pulseira Tissot", "brand": "Tissot"}),
    )
    assert result.safety_reason == "service_intent_clarification"
    sales._handle_sales_catalog_inner.assert_not_called()
