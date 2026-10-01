"""Read-only replay of a named-SKU/warranty/first-purchase-discount incident."""
from unittest.mock import AsyncMock

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.answer_council import apply_answer_council_with_retry
from app.sales.product_policy import complete_product_policy_answer
from app.verify.final_response import finalize_response, result_interpretation

ASK = ("Boa noite estou interessado no Tissot PRX powermatic 80 35mm azul "
       "gostaria de saber se vem com a garantia da tissot ativa e todas as documentações")
DISCOUNT = "Algum desconto pra primeira compra?"


def inspect():
    return SalesInterpretation(domain="commerce", goal="inspect",
        subject={"brand": "Tissot", "model": "PRX Powermatic 80"},
        preferences={"color": "azul", "mechanism": "automatic", "attributes": ["case_size_mm:35"]},
        information_needed=["catalog"], answer_strategy="search_catalog",
        needs_clarification=False, enough_information_to_search=True, ready_for_retrieval=True,
        references_previous_context=False, confidence=.99)


def discount():
    return inspect().model_copy(update={"goal": "buy", "payment_action": "payment_options",
        "payment_request_kind": "informational", "purchase_stage": "selection",
        "information_needed": [], "answer_strategy": "answer_directly"})


@pytest.fixture
def offline(monkeypatch):
    import app.sales_agent as sales
    monkeypatch.setattr(sales, "execute_tool", AsyncMock(side_effect=AssertionError("no live tools")))
    monkeypatch.setattr("app.llm.openai_gateway.generate_text_output",
                        AsyncMock(side_effect=AssertionError("no paid AI")))


@pytest.mark.asyncio
async def test_four_turn_replay_preserves_policy_through_final_outbound(offline, monkeypatch):
    import app.sales_agent as sales
    from app.catalog.retrieval.technical import technical_miss
    from app.ops.handoff_service import enrich_handoff_metadata
    from app.llm.agent_contracts import build_agent_decision
    from app.verify.factual_validator import apply_factual_validation
    from app.verify.double_check import apply_double_check
    from app.llm.response_composer import compose_outbound_reply
    calls = []

    async def retrieve(interp, **kwargs):
        calls.append(interp.model_copy(deep=True))
        assert interp.subject.model == "PRX Powermatic 80"
        assert interp.preferences.color == "azul"
        assert "case_size_mm:35" in interp.preferences.attributes
        return technical_miss(interp, unknown=True)

    monkeypatch.setattr(sales, "_execute_compiled_product_retrieval", retrieve)
    state = CommerceConversationState(dialogue_phase="shortlist",
        active_preferences={"subject_brand": "Tissot", "subject_model": "PRX Powermatic 80", "color": "azul"})
    history = []
    for text, interp in [(ASK, inspect()), (DISCOUNT, discount()), (ASK, inspect()), (DISCOUNT, discount())]:
        incoming = IncomingMessage(text=text, channel="instagram")
        before = state.model_dump()
        result = await sales.handle_sales_message(incoming, {}, {}, interp, history, state)
        assert result is not None
        result, report, used_interp = await apply_answer_council_with_retry(result,
            incoming=incoming, interpretation=result_interpretation(result) or interp, commerce_state=state)
        result = apply_factual_validation(result, decision=build_agent_decision(incoming, result, openai_call_count=0),
            mode="enforce", commerce_state=state.model_dump(mode="json"))
        assert result.response_metadata["factual_validation"]["valid"], result.response_metadata["factual_validation"]["violations"]
        result, check = apply_double_check(incoming=incoming, result=result, commerce_state=state, mode="enforce")
        assert not check.applied
        result = enrich_handoff_metadata(incoming, result, recent_turns=history)
        result = compose_outbound_reply(incoming, result, max_reply_chars=900)
        result, state = finalize_response(result, incoming=incoming,
            interpretation=used_interp, previous_state=state)
        result = enrich_handoff_metadata(incoming, result, recent_turns=history)
        assert not result.handoff_required
        assert result.response_metadata["final_response_validation"]["passed"]
        assert "1, 2 ou 3" not in result.reply_text
        assert "preciso da ajuda de um atendente" not in result.reply_text
        assert state.cart_session_id is None and state.order_id is None
        if text == ASK:
            assert "defeitos de fabricação" in result.reply_text
            assert "garantia ativa diretamente com o fabricante não está confirmada" in result.reply_text
            assert "lista completa de documentos" in result.reply_text
            assert result.commercial_data["products"] == []
            assert result.response_metadata["institutional_evidence"]
        else:
            assert "PIX" in result.reply_text and "primeira compra" in result.reply_text
            assert state.model_dump() == before
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": result.reply_text}]
    assert len(calls) == 2
    sales.execute_tool.assert_not_called()


@pytest.mark.parametrize("phase,pending", [("shortlist", None), ("buy", "show_images"),
    ("buy", "send_product_link"), ("discovery", "show_nearby_line")])
def test_browsing_product_is_not_a_checkout(phase, pending):
    from app.sales.turn_contract import inbound_from_memory
    state = CommerceConversationState(dialogue_phase=phase, pending_action=pending,
        active_product={"product_id": "watch"}, last_presented_products=[{"product_id": "watch", "position": 1}])
    view = inbound_from_memory(inspect(), state)
    assert view.live_shortlist and not view.live_checkout


@pytest.mark.parametrize("pending", ["choose_checkout_channel", "awaiting_shipping_zipcode", "awaiting_checkout_data"])
def test_selected_target_in_checkout_stays_checkout(pending):
    from app.sales.turn_contract import inbound_from_memory
    state = CommerceConversationState(pending_action=pending, active_product={"product_id": "watch"})
    assert inbound_from_memory(inspect(), state).live_checkout


def test_policy_query_never_becomes_list_selection():
    from app.sales.purchase_selection import parse_list_position_reference, repair_presented_purchase_selection
    from app.sales.intent_router import route_sales_intent
    state = CommerceConversationState(last_presented_products=[{"product_id": "watch", "position": 1}])
    assert parse_list_position_reference(DISCOUNT) is None
    assert parse_list_position_reference("Quero o primeiro") == 1
    interp = discount()
    assert repair_presented_purchase_selection(interp, message_text=DISCOUNT, state=state) is interp
    route = route_sales_intent(interpretation=interp, plan={"intent": "clarification", "query": ""},
        message_text=DISCOUNT, commerce_state=state)
    assert not route.purchase_close_hold and route.route_kind == "talk"


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_selection_prompt_only_uses_real_options(count):
    from app.sales_agent import _purchase_close_hold_reply
    state = CommerceConversationState(last_presented_products=[
        {"product_id": str(i), "position": i, "name": "Produto " + str(i)} for i in range(1, count + 1)])
    reply = _purchase_close_hold_reply(message=IncomingMessage(text="Quero comprar"),
        state=state, interpretation=inspect().model_copy(update={"goal": "buy"}))
    assert "1, 2 ou 3" not in reply
    for i in range(1, count + 1):
        assert "Produto " + str(i) in reply
    if not count:
        assert "lista" not in reply


@pytest.mark.parametrize("pct", [0, 7, 15])
def test_discount_comes_from_runtime_and_keeps_cart_and_product_memory(pct):
    from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
    from app.sales.policies.action_authority import try_informational_payment
    token = set_persona_runtime(PersonaRuntimeConfig(pix_discount_percent=pct, max_pix_discount_percent=pct))
    try:
        state = CommerceConversationState(cart_session_id="cart", cart_product_id="watch",
            active_product={"product_id": "watch"}, pending_action="awaiting_checkout_data",
            active_preferences={"color": "azul"})
        incoming = IncomingMessage(text=DISCOUNT)
        result = try_informational_payment(incoming, discount(), state)
        final, updated = finalize_response(result, incoming=incoming, interpretation=discount(), previous_state=state)
        assert f"{pct}%" in final.reply_text
        assert updated == state and updated is not state
    finally:
        reset_persona_runtime(token)


@pytest.mark.parametrize("updates,text", [
    ({"purchase_action": "create_cart"}, "Quero comprar o 2 no PIX"),
    ({"purchase_action": "remove_cart_item"}, "Remove o segundo, tem desconto?"),
    ({"purchase_action": "set_cart_item_quantity"}, "Muda para duas unidades, tem desconto?"),
    ({"payment_request_kind": "checkout"}, "Vou pagar no PIX"),
    ({"installment_count": 10}, "Quanto fica em 10 vezes?"),
    ({"information_needed": ["catalog"]}, "Qual o valor deste PRX com desconto?"),
])
def test_payment_policy_does_not_capture_checkout_or_simulation(updates, text):
    from app.sales.policies.action_authority import try_informational_payment
    assert try_informational_payment(IncomingMessage(text=text), discount().model_copy(update=updates),
                                     CommerceConversationState()) is None


def test_policy_composition_never_revives_a_rejected_offer():
    from app.catalog.retrieval.technical import technical_miss
    interp = inspect()
    result = technical_miss(interp, unknown=True)
    result.reply_text = "Tissot errado por R$ 100 com garantia ativa!"
    result.commercial_data["products"] = [{"id": "fake", "price": 100}]
    result = complete_product_policy_answer(result, interp, ASK)
    assert result.commercial_data["products"] == []
    assert "R$ 100" not in result.reply_text and "garantia ativa!" not in result.reply_text
    assert "defeitos de fabricação" in result.reply_text
    repeated = complete_product_policy_answer(result, interp, ASK)
    assert repeated.reply_text == result.reply_text


@pytest.mark.parametrize("manufacturer_active", [None, True])
def test_specific_warranty_requires_specific_evidence(manufacturer_active):
    from app.catalog.retrieval.offer_contract import seal_offer
    product = {"id": "verified", "name": "Tissot PRX Powermatic 80 azul 35mm", "brand": "Tissot",
        "price": 5000, "available": True, "_revalidated": True, "warranty": "24 meses",
        "included_documents": "Manual e certificado", "manufacturer_warranty_active": manufacturer_active}
    result = seal_offer(AgentResult(reply_text="Produto consultado", commercial_data={"products": [product]}))
    fixed = complete_product_policy_answer(result, inspect(), ASK)
    assert "24 meses" in fixed.reply_text and "Manual e certificado" in fixed.reply_text
    assert ("informa garantia ativa do fabricante" in fixed.reply_text) is bool(manufacturer_active)
    assert len(fixed.commercial_data["products"]) == 1
    from app.llm.agent_contracts import build_agent_decision
    from app.verify.factual_validator import apply_factual_validation
    incoming = IncomingMessage(text=ASK)
    fixed = apply_factual_validation(fixed, decision=build_agent_decision(incoming, fixed, openai_call_count=0), mode="enforce")
    final, state = finalize_response(fixed, incoming=incoming, interpretation=inspect(), previous_state=CommerceConversationState())
    assert "24 meses" in final.reply_text and "Manual e certificado" in final.reply_text
    assert [p.product_id for p in state.last_presented_products] == ["verified"]


@pytest.mark.parametrize("change", ["text_rate", "policy_rate", "extra_coupon", "flag_only"])
def test_discount_authority_does_not_accept_fabricated_claims(change):
    from app.sales.policies.action_authority import try_informational_payment, verified_informational_payment_reply
    from app.verify.factual_validator import validate_factual_response
    from app.llm.agent_contracts import build_agent_decision
    incoming = IncomingMessage(text=DISCOUNT)
    result = try_informational_payment(incoming, discount(), CommerceConversationState())
    assert verified_informational_payment_reply(result)
    if change == "text_rate":
        result.reply_text = "Tem desconto de 99% no PIX."
    elif change == "policy_rate":
        result.commercial_data["payment_policy"]["pix_discount_percent"] = 99
    elif change == "extra_coupon":
        result.reply_text += " Tem mais um desconto de 10% com cupom PRIMEIRA10."
    else:
        result.response_metadata.pop("payment_policy_question")
    assert not verified_informational_payment_reply(result)
    report = validate_factual_response(result, decision=build_agent_decision(incoming, result, openai_call_count=0))
    assert any(v.kind == "promo" for v in report.violations)


def test_requested_human_and_after_sales_keep_their_routes():
    from app.sales.product_policy import normalize_product_policy_lookup
    from app.sales.policies.action_authority import try_informational_payment
    interp = inspect().model_copy(update={"answer_strategy": "handoff"})
    assert normalize_product_policy_lookup("Quero falar com um atendente sobre a garantia do Tissot PRX", interp) is interp
    assert try_informational_payment(IncomingMessage(text="Quero falar com um atendente sobre desconto"), discount(), CommerceConversationState()) is None
    after_sales = inspect().model_copy(update={"goal": "after_sales"})
    assert normalize_product_policy_lookup("Meu Tissot PRX quebrou, preciso da garantia", after_sales) is after_sales


def test_known_sku_inspection_does_not_lose_existing_specification_answer():
    from app.catalog.retrieval.offer_contract import seal_offer
    product = {"id": "known", "reference": "known-ref", "name": "PRX", "price": 5000,
        "description": "Diâmetro: 35 mm\nVidro: safira", "_revalidated": True}
    result = seal_offer(AgentResult(reply_text="Ficha consultada", commercial_data={"products": [product]},
        response_metadata={"identity_inspection": True}))
    fixed = complete_product_policy_answer(result, inspect(), "Tem vidro de safira e garantia?")
    assert "Vidro: cristal de safira" in fixed.reply_text
    assert "garantia" in fixed.reply_text


def test_published_coverage_is_not_hardcoded(monkeypatch):
    from app.catalog.retrieval.technical import technical_miss
    from app.persona.store_knowledge import EvidencePackage
    monkeypatch.setattr("app.persona.store_knowledge.fetch_institutional_knowledge", lambda _: EvidencePackage(items=[
        {"slug": "garantia-e-cuidados", "title": "Política atual", "body": "## Cobertura\n\nCondições atualizadas pela loja.\n\n## Regra para o agente\nTexto interno."}]))
    fixed = complete_product_policy_answer(technical_miss(inspect(), unknown=True), inspect(), ASK)
    assert "Condições atualizadas pela loja" in fixed.reply_text
    assert "defeitos de fabricação" not in fixed.reply_text
    assert "Texto interno" not in fixed.reply_text
