from unittest.mock import AsyncMock

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.ops.turn_runtime import TurnRuntimeContext, LLMCallBudget
from app.ops.runtime_context import set_current_turn, reset_current_turn
from app.verify.response_critique import CritiqueVerdict, apply_response_critique_loop
from app.verify.final_response import finalize_response
from tests.catalog.test_technical_requirements import interpretation, ASK
from app.catalog.specs.requirements import normalize_requirements


def draft():
    interp = interpretation(); normalize_requirements(interp, ASK)
    product = {"id":"ok", "name":"Modelo confirmado", "reference":"TEST-123", "description":"Automático com cristal de safira",
               "price":2200, "stock":3, "available":True, "_revalidated":True, "_factual_source":"tray_live"}
    return AgentResult(reply_text="Modelo confirmado por R$ 2.200,00", intent="commerce", commercial_data={"products":[product]},
        response_metadata={"interpretation":interp.model_dump(mode="json"), "presented_products":True,
                           "product_resolution_state":"options_presented", "dialogue_phase":"shortlist", "factual_validation":{"valid":True}})


@pytest.mark.asyncio
async def test_missing_api_key_is_unavailable_never_approved(monkeypatch):
    from types import SimpleNamespace
    from app.verify.response_critique import run_critique_judge
    monkeypatch.setattr("app.verify.response_critique.get_settings", lambda: SimpleNamespace(openai_api_key=""))
    verdict = await run_critique_judge(incoming=IncomingMessage(text=ASK), result=draft(), recent_turns=[], commerce_state=None)
    assert verdict._execution_error == "openai_not_configured"
    assert not verdict.pass_check


@pytest.mark.asyncio
async def test_deterministic_draft_releases_reserved_slot_for_review(monkeypatch):
    runtime = TurnRuntimeContext(trace_id="test", llm_budget=LLMCallBudget(max_calls=3,used_calls=2,enforce=True))
    token = set_current_turn(runtime)
    async def judge(**kwargs):
        runtime.register_openai_call("judge")
        return CritiqueVerdict(pass_check=True)
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    try:
        result, report = await apply_response_critique_loop(incoming=IncomingMessage(text=ASK), result=draft(), mode="enforce")
        assert report.approved is True
        assert runtime.llm_budget.used_calls == 3
        assert not result.handoff_required
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [True, False])
async def test_reviewer_unavailable_is_not_rejection_or_regeneration(monkeypatch, exhausted):
    runtime = TurnRuntimeContext(trace_id="test", llm_budget=LLMCallBudget(max_calls=3,used_calls=3 if exhausted else 1,enforce=True))
    token = set_current_turn(runtime)
    verdict = CritiqueVerdict(pass_check=False)
    verdict._execution_error = "OpenAIGatewayError"
    judge = AsyncMock(return_value=verdict)
    regenerate = AsyncMock(side_effect=AssertionError("operational failure must not regenerate"))
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    monkeypatch.setattr("app.verify.response_critique._regenerate_reply", regenerate)
    try:
        result, report = await apply_response_critique_loop(incoming=IncomingMessage(text=ASK), result=draft(), mode="enforce")
        assert report.approved is None
        assert report.review_status == "unavailable"
        assert report.verdicts == []
        assert not report.regenerated
        assert not result.handoff_required
        assert "Modelo confirmado" in result.reply_text
        regenerate.assert_not_awaited()
        assert judge.await_count == (0 if exhausted else 1)
    finally:
        reset_current_turn(token)


def test_handoff_cannot_persist_unseen_shortlist():
    result = draft(); result.handoff_required = True; result.reply_text = "Vou solicitar ajuda da equipe."
    interp = interpretation()
    final, state = finalize_response(result, incoming=IncomingMessage(text=ASK), interpretation=interp, previous_state=CommerceConversationState())
    assert not final.response_metadata["presented_products"]
    assert final.response_metadata["product_resolution_state"] == "handoff"
    assert state.last_presented_products == []
    assert state.active_product is None
    assert state.dialogue_phase == "discovery"
    assert final.commercial_data["products"] == []


def test_last_stage_blocks_incompatible_product_even_after_approved_review():
    result = draft()
    result.commercial_data["products"][0]["description"] = "Quartzo com cristal mineral"
    final, state = finalize_response(result, incoming=IncomingMessage(text=ASK), interpretation=interpretation(), previous_state=CommerceConversationState())
    assert final.commercial_data["products"] == []
    assert not state.last_presented_products
    assert "final_technical_requirements_failed" in final.response_metadata["final_response_validation"]["rejected_issues"]


def test_only_products_identified_in_sent_text_enter_memory():
    result = draft()
    result.commercial_data["products"].append({**result.commercial_data["products"][0],"id":"hidden","name":"Modelo oculto","reference":"TEST-456"})
    final, state = finalize_response(result, incoming=IncomingMessage(text=ASK), interpretation=interpretation(), previous_state=CommerceConversationState())
    assert [p.product_id for p in state.last_presented_products] == ["ok"]
    assert final.response_metadata["final_response_validation"]["delivered_product_ids"] == ["ok"]


def test_catalog_name_without_generic_watch_prefix_enters_memory():
    interp = SalesInterpretation(
        domain='commerce', goal='inspect', confidence=.99,
        needs_clarification=False,
        references_previous_context=False,
        subject={'brand':'Baltic','model':'MK2','product_type':'relógio'},
        preferences={'attributes':['case_size:37-37mm']},
    )
    product = {
        'id':'14738', 'brand':'Baltic',
        'name':'Relógio Baltic Aquascaphe MK2 Automático Cinza 37mm',
        'price':7199.99, 'available':True, '_revalidated':True,
        '_factual_source':'tray_live',
    }
    result = AgentResult(
        reply_text='Encontrei o Baltic Aquascaphe MK2 Automático Cinza 37mm.',
        intent='commerce', commercial_data={'products':[product]},
        response_metadata={
            'interpretation':interp.model_dump(mode='json'),
            'presented_products':True,
            'product_resolution_state':'found_available',
            'domain':'commerce',
        },
    )

    final, state = finalize_response(
        result,
        incoming=IncomingMessage(text='Quero saber o preço do Baltic MK2 37mm'),
        interpretation=interp,
        previous_state=CommerceConversationState(),
    )

    assert final.response_metadata['final_response_validation']['delivered_product_ids'] == ['14738']
    assert state.last_presented_products[0].product_id == '14738'


@pytest.mark.asyncio
async def test_last_enrichment_cannot_restore_hidden_products(monkeypatch, approved_critique):
    from types import SimpleNamespace
    import app.message_pipeline as pipeline
    monkeypatch.setattr(pipeline,"get_settings",lambda:SimpleNamespace(audio_inbound_enabled=False,audio_outbound_enabled=False))
    monkeypatch.setattr(pipeline,"load_commerce_conversation_state",lambda **kwargs:{})
    monkeypatch.setattr(pipeline,"generate_agent_reply_async",AsyncMock(return_value=draft()))
    async def enrich(incoming,result):
        result.reply_text = "Vou solicitar ajuda da equipe."
        result.handoff_required = True
        return result
    monkeypatch.setattr(pipeline,"enrich_agent_result",enrich)
    result = await pipeline.process_incoming_message(IncomingMessage(text=ASK),{})
    assert not result.handoff_required
    assert result.response_metadata['handoff']['offer']
    assert result.response_metadata["commerce_state"]["last_presented_products"] == []
    assert not result.response_metadata["presented_products"]
    assert result.response_metadata["final_response_validation"]["delivered_product_ids"] == []
