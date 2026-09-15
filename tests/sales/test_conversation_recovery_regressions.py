from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.commerce.commerce_context import CommerceConversationState, CommerceProductReference, evolve_commerce_state
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.catalog.specs.preference_normalize import normalize_sales_interpretation
from app.sales.catalog_reference import resolve_catalog_reference
from app.sales.conversation_repair import repair_conversation
from app.sales.dialogue_phase import reconcile_checkout_context
from app.verify.double_check import collect_phase1_risk_signals


def interpretation(strategy="search_catalog"):
    return SalesInterpretation(domain="commerce", goal="find", confidence=.97, references_previous_context=True,
        subject={"brand": "Orient", "model": "Open Heart"}, preferences={"color": "preto", "budget_max": 3500},
        reference_type="current_product", answer_strategy=strategy, ready_for_retrieval=True,
        enough_information_to_search=True, stop_clarification=True, needs_clarification=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "não é esse relogio que queria, eu quero o orient open heart preto",
    "qual o preço desse Orient Open Heart preto?",
])
async def test_explicit_model_survives_reference_guard(text):
    state = CommerceConversationState(active_product=CommerceProductReference(product_id="old", name="Orient Kanno"))
    result = await resolve_catalog_reference(message=IncomingMessage(text=text), interpretation=interpretation(), plan={}, state=state)
    assert result.early_result is None
    assert result.resolved_product is None
    assert result.interpretation.subject.model == "Open Heart"


@pytest.mark.asyncio
async def test_ambiguous_real_price_still_waits_for_reference():
    result = await resolve_catalog_reference(message=IncomingMessage(text="qual o preço desse?"),
        interpretation=interpretation(), plan={}, state=CommerceConversationState())
    assert result.early_result is not None
    assert result.early_result.response_metadata["fallback_reason"] == "deictic_price_without_image"


@pytest.mark.parametrize("text", ["que pergunta de preço?", "não perguntei o preço", "não foi isso que perguntei"])
def test_meta_correction_is_not_price_request(text):
    signals = collect_phase1_risk_signals(incoming=IncomingMessage(text=text),
        result=AgentResult(reply_text="Entendi", intent="commerce"))
    assert "inbound_asks_price" not in signals


def test_real_price_keeps_verification():
    assert "inbound_asks_price" in collect_phase1_risk_signals(incoming=IncomingMessage(text="qual o preço?"),
        result=AgentResult(reply_text="R$ 2.000", intent="commerce"))


def test_model_normalization_is_idempotent():
    item = interpretation()
    item.subject.model = "Open Heart Open Heart Open Heart"
    for _ in range(4):
        item = normalize_sales_interpretation(item, message_text="quero o Orient Open Heart preto")
        assert item.subject.model == "Open Heart"


@pytest.mark.asyncio
async def test_complaint_searches_known_model_and_repeat_escalates(monkeypatch):
    query = AsyncMock(return_value=AgentResult(reply_text="Resultado verificado", intent="commerce",
                                              response_metadata={"used_tray": True}))
    monkeypatch.setattr("app.sales.product_lookup.execute_compiled_product_retrieval", query)
    state = CommerceConversationState(active_preferences={"subject_brand": "Orient", "subject_model": "Open Heart",
        "color": "preto", "budget_max": 3500, "material": "safira"})
    reply = await repair_conversation(incoming=IncomingMessage(text="que pergunta de preço?"),
        interpretation=interpretation("acknowledge"), state=state)
    assert query.await_count == 1
    actual = query.call_args.args[0]
    assert actual.answer_strategy == "search_catalog"
    assert actual.preferences.material == "safira"
    assert not actual.purchase_action
    assert "Resultado verificado" in reply.reply_text
    state = evolve_commerce_state(state, reply)
    assert state.conversation_repair_attempts == 1
    again = await repair_conversation(incoming=IncomingMessage(text="ta entendendo nada"),
        interpretation=interpretation("acknowledge"), state=state)
    assert again.handoff_required
    assert query.await_count == 1
    assert again.response_metadata["active_preferences"]["color"] == "preto"


def test_fallback_without_preferences_preserves_all_known_criteria():
    before = CommerceConversationState(active_preferences={"subject_brand": "Orient", "color": "preto", "budget_max": 3500})
    after = evolve_commerce_state(before, AgentResult(reply_text="Entendi", intent="commerce", response_metadata={"domain": "commerce"}))
    assert after.active_preferences == before.active_preferences


def test_orphaned_cart_is_cleared_but_preferences_are_kept():
    state = CommerceConversationState(cart_session_id="old", dialogue_phase="checkout", pending_action="choose_checkout_channel",
                                     active_preferences={"subject_model": "Open Heart Open Heart", "color": "preto"})
    cleaned = reconcile_checkout_context(state)
    assert not cleaned.cart_session_id and not cleaned.pending_action
    assert cleaned.dialogue_phase == "discovery"
    assert cleaned.active_preferences == {"subject_model": "Open Heart", "color": "preto"}
    assert cleaned.context_repairs == ["orphaned_cart"]
    assert state.cart_session_id == "old"


def test_cart_expiry_never_erases_real_order():
    now = datetime.now(timezone.utc)
    state = CommerceConversationState(cart_session_id="old", cart_product_id="sku", cart_context_updated_at=now-timedelta(days=2))
    assert reconcile_checkout_context(state, now=now).cart_session_id is None
    state.order_id = "order"
    assert reconcile_checkout_context(state, now=now).cart_session_id == "old"


@pytest.mark.asyncio
async def test_council_rejected_fallback_cannot_requalify(monkeypatch):
    from app.sales.answer_council import apply_answer_council_with_retry, check_pedido, build_turn_contract
    monkeypatch.setattr("app.sales.product_lookup.execute_compiled_product_retrieval", AsyncMock(return_value=None))
    original = AgentResult(reply_text="Qual a marca e faixa de investimento?", intent="commerce")
    interp = interpretation()
    state = CommerceConversationState(active_preferences={"subject_brand":"Orient", "subject_model":"Open Heart"})
    result, decision, _ = await apply_answer_council_with_retry(original,
        incoming=IncomingMessage(text="quero o orient open heart preto"), interpretation=interp, commerce_state=state)
    assert result.handoff_required
    assert result.response_metadata["interpretation"]["subject"]["model"] == "Open Heart"
    contract = build_turn_contract(message_text="quero o orient open heart preto", interpretation=interp, commerce_state=state)
    assert "requalify_after_sku" not in check_pedido(result, contract).issues
