"""Availability shortcuts must still retain the turn and respect repair feedback."""
from unittest.mock import AsyncMock

import pytest

from app.agents import door
from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.llm.turn_understanding import TurnUnderstanding, turn_understanding_to_sales, sales_to_turn_understanding
from app.models import AgentResult, IncomingMessage


def interpretation(feedback=None):
    understood = TurnUnderstanding(primary_intent="commerce_find", user_goal="Relógio para casamento", confidence=0.98,
        answer_strategy="search_catalog", conversation_feedback=feedback,
        soft_preferences={"occasion": "casamento", "style": "social", "availability": "pronta entrega",
                          "urgency": "próximo final de semana"})
    return turn_understanding_to_sales(understood)


@pytest.mark.asyncio
async def test_ready_delivery_understands_constraints_before_shortcut(monkeypatch):
    understood = AsyncMock(return_value=interpretation())
    lookup = AsyncMock(return_value=AgentResult(reply_text="Opções anunciadas na lista.", intent="sales",
        response_metadata={"domain": "commerce", "response_source": "ready_delivery_storefront"}))
    monkeypatch.setattr(door, "interpret_message", understood)
    monkeypatch.setattr(door, "load_recent_conversation_turns", lambda **kw: [])
    monkeypatch.setattr("app.persona.institutional_route.answer_institutional", AsyncMock(return_value=None))
    monkeypatch.setattr("app.sales.ready_delivery.try_ready_delivery", lookup)
    monkeypatch.setattr(door, "handle_sales_message", AsyncMock(side_effect=AssertionError("No discovery interview")))
    incoming = IncomingMessage(text="Preciso de um social a pronta entrega para um casamento no próximo final de semana",
        channel="whatsapp", conversation_id="fixture-thread", raw={"inbound_id": 5, "timestamp": "2026-10-01T21:00:00Z"})
    result = await door._generate_agent_reply_async_inner(incoming, {})
    understood.assert_awaited_once()
    lookup.assert_awaited_once()
    state = evolve_commerce_state(CommerceConversationState(), result)
    assert state.active_preferences["occasion"] == "casamento"
    assert state.active_preferences["style"] == "social"
    assert state.active_preferences["delivery_mode"] == "ready_to_ship"
    assert state.delivery_requirement["earliest_date"] == "2026-10-03"
    assert state.delivery_requirement["latest_date"] == "2026-10-04"
    assert state.delivery_requirement["arrival_confirmed"] is False
    assert state.preference_provenance["occasion"]["inbound_id"] == 5


@pytest.mark.asyncio
async def test_ready_delivery_context_cannot_bypass_feedback_repair(monkeypatch):
    monkeypatch.setattr(door, "interpret_message", AsyncMock(return_value=interpretation("frustrated")))
    lookup = AsyncMock(side_effect=AssertionError("Feedback must not repeat the ready-delivery list"))
    monkeypatch.setattr("app.sales.ready_delivery.try_ready_delivery", lookup)
    repaired = AsyncMock(return_value=AgentResult(reply_text="Entendi a correção.", intent="commerce"))
    monkeypatch.setattr(door, "handle_sales_message", repaired)
    await door._route_after_interpret(message=IncomingMessage(text="Pqp, já falei para que preciso"),
        customer_context={}, commerce_state=CommerceConversationState(ready_delivery_context={"query": "relógio"}),
        recovery_turns=[], recent_turns=[], model_turns=[], answering_qualification=False, phrase_restart=False)
    lookup.assert_not_awaited()
    repaired.assert_awaited_once()


def test_availability_and_urgency_survive_adapter_round_trip():
    result = turn_understanding_to_sales(sales_to_turn_understanding(interpretation()))
    assert result.preferences.delivery_mode == "ready_to_ship"
    assert result.preferences.delivery_deadline_text == "próximo final de semana"


@pytest.mark.asyncio
async def test_ready_category_followup_passes_previously_declared_style_to_shortcut(monkeypatch):
    from app.sales.ready_delivery import contextual_listing_query
    parsed = turn_understanding_to_sales(TurnUnderstanding(primary_intent='commerce_find',
        user_goal='Consultar categoria pronta entrega', confidence=.95, references_previous_context=True,
        soft_preferences={'availability':'pronta entrega'}, answer_strategy='search_catalog'))
    monkeypatch.setattr(door, 'interpret_message', AsyncMock(return_value=parsed))
    seen = []
    async def listing(message, state, *, interpretation, recent_turns):
        seen.append(interpretation)
        assert interpretation.preferences.style == 'social'
        assert contextual_listing_query(message, 'pronta entrega', interpretation) is None
        return None
    monkeypatch.setattr('app.sales.ready_delivery.try_ready_delivery', listing)
    monkeypatch.setattr(door, 'handle_sales_message', AsyncMock(return_value=AgentResult(reply_text='Consulta contextual',intent='commerce')))
    await door._route_after_interpret(message=IncomingMessage(text='Categoria pronta entrega'), customer_context={},
        commerce_state=CommerceConversationState(active_domain='commerce', active_preferences={'style':'social'}),
        recovery_turns=[], recent_turns=[], model_turns=[], answering_qualification=False, phrase_restart=False)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('text,question', [('Gostaria de uma informação','Sobre qual assunto você quer informação?'),
    ('52998224725','Você quer consultar um pedido com esse CPF?')])
async def test_failed_prose_uses_interpreted_missing_information_instead_of_new_greeting(monkeypatch,text,question):
    parsed = turn_understanding_to_sales(TurnUnderstanding(primary_intent='store_general',user_goal='Esclarecer finalidade',
        confidence=.95,answer_strategy='clarify',clarification_required=True,clarification_question=question))
    monkeypatch.setattr(door,'interpret_message',AsyncMock(return_value=parsed))
    monkeypatch.setattr(door,'generate_openai_reply_async',AsyncMock(return_value=AgentResult(reply_text='Olá! Como posso ajudar?',
        intent='general_support',safety_reason='tools_request_failed')))
    result=await door._route_after_interpret(message=IncomingMessage(text=text),customer_context={},
        commerce_state=CommerceConversationState(),recovery_turns=[],recent_turns=[],model_turns=[],
        answering_qualification=False,phrase_restart=False)
    assert result.reply_text == question
    assert result.safety_reason == 'contextual_clarification_fallback'
    assert result.response_metadata['used_openai_responder'] is False
