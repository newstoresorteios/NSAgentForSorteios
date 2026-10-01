"""Sanitized incident replays from 2026-10-01; no model or production services."""
import pytest

from app.models import AgentResult, IncomingMessage
from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.commerce.order_context_recovery import extract_handles_from_conversation
from app.commerce.context_boundaries import ambiguous_number_question
from app.sales.discovery import _discovery_state
from tests.sales.test_contextual_discovery import contextual, interpretation, marker


@pytest.mark.parametrize('document', ['11144477735', '111.444.777-35'])
def test_mixed_order_prompt_does_not_turn_cpf_into_order_or_budget(document):
    turns = [{'role': 'assistant', 'content': 'Informe o número do pedido, CPF ou e-mail da compra.'}]
    handles = extract_handles_from_conversation(
        state=CommerceConversationState(), recent_turns=turns, message_text=document)
    assert handles['documents'] == [('cpf', '11144477735')]
    assert handles['order_ids'] == []
    assert ambiguous_number_question(document, turns, handles) is None


@pytest.mark.asyncio
async def test_document_answer_uses_customer_lookup_after_mixed_prompt(monkeypatch):
    from app.agents.door_order import try_tax_document_route
    seen = []
    async def lookup(**kwargs):
        seen.append((kwargs['document_kind'], kwargs['document']))
        return AgentResult(reply_text='Pedido localizado.', intent='commerce')
    monkeypatch.setattr('app.agents.door.find_order_by_customer_document', lookup)
    result = await try_tax_document_route(
        IncomingMessage(text='11144477735'), CommerceConversationState(),
        recent_turns=[{'role': 'assistant', 'content': 'Qual o número do pedido, CPF ou e-mail?'}])
    assert result is not None
    assert seen == [('cpf', '11144477735')]


def test_incremental_answer_preserves_wedding_budget_and_ready_delivery():
    state = CommerceConversationState(active_preferences={
        'occasion': 'casamento', 'style': 'social', 'budget_max': 5000,
        'attributes': ['ready_to_ship', 'qual:urgency:próximo fim de semana']})
    result = AgentResult(reply_text='Entendi.', intent='commerce', response_metadata={
        'domain': 'commerce', 'active_preferences': {'color': 'azul'}})
    updated = evolve_commerce_state(state, result)
    assert updated.active_preferences['budget_max'] == 5000
    assert updated.active_preferences['occasion'] == 'casamento'
    assert updated.active_preferences['style'] == 'social'
    assert updated.active_preferences['color'] == 'azul'
    assert 'ready_to_ship' in updated.active_preferences['attributes']
    assert state.active_preferences.get('color') is None


def test_preference_correction_replaces_only_corrected_dimension():
    state = CommerceConversationState(active_preferences={'color': 'azul', 'budget_max': 5000, 'style': 'social'})
    result = AgentResult(reply_text='Preto, combinado.', intent='commerce', response_metadata={
        'domain': 'commerce', 'active_preferences': {'color': 'preto', 'explicit_no_preferences': ['budget']}})
    updated = evolve_commerce_state(state, result)
    assert updated.active_preferences['color'] == 'preto'
    assert updated.active_preferences['style'] == 'social'
    assert not updated.active_preferences.get('budget_max')


def test_intervening_fallback_does_not_restart_qualification(contextual):
    history = [marker('model_intent'), marker('budget'), marker('occasion'),
               {'role': 'assistant', 'content': 'Não consegui consultar agora.',
                'metadata': {'safety_reason': 'catalog_unavailable'}}]
    state = _discovery_state(interpretation(), history, message_text='Pode continuar')
    assert not state['persona_qualification_required']
    assert state['force_retrieval']


@pytest.mark.parametrize('text', [
    'Já falei que quero saber quais você tem em estoque',
    'Quais modelos vocês têm a pronta entrega?',
])
def test_explicit_inventory_request_does_not_restart_interview(contextual, text):
    state = _discovery_state(interpretation(), [], message_text=text)
    assert not state['persona_qualification_required']
    assert state['force_retrieval']


def test_repair_attempt_survives_intermediate_reply():
    state = CommerceConversationState(conversation_repair_attempts=1)
    updated = evolve_commerce_state(state, AgentResult(reply_text='Entendi.', intent='commerce',
        response_metadata={'domain': 'commerce'}))
    assert updated.conversation_repair_attempts == 1


@pytest.mark.parametrize('text', ['Como faço para falar com um atendente?', 'Como posso conversar com uma pessoa?'])
def test_explicit_human_request_is_consent(text):
    from app.ops.handoff_consent import customer_requests_human
    assert customer_requests_human(text)


def test_negative_human_request_is_not_consent():
    from app.ops.handoff_consent import customer_requests_human
    assert not customer_requests_human('Não quero falar com um atendente')


@pytest.mark.asyncio
async def test_cpf_alone_locates_one_owned_order_without_leaking_other_customers():
    from app.commerce.order_service import find_order_by_customer_document
    calls = []
    async def execute(tool, args):
        calls.append((tool, args))
        if tool == 'search_customer':
            return {'customers': [{'id': 'customer-1'}]}
        if tool == 'list_orders':
            return {'orders': [{'id': 'order-9', 'customer_id': 'customer-1', 'status': 'ENVIADO'},
                               {'id': 'private', 'customer_id': 'someone-else'}]}
        if tool == 'get_order_complete':
            return {'order_id': 'order-9', 'customer_id': 'customer-1', 'status': 'ENVIADO'}
        raise AssertionError(tool)
    result = await find_order_by_customer_document(state=CommerceConversationState(), execute=execute,
        document_kind='cpf', document='11144477735')
    assert result.commercial_data['order_id'] == 'order-9'
    assert 'private' not in str(result.model_dump())
    assert calls[-1] == ('get_order_complete', {'order_id': 'order-9'})


@pytest.mark.asyncio
async def test_cpf_with_multiple_owned_orders_requests_only_customer_choice():
    from app.commerce.order_service import find_order_by_customer_document
    async def execute(tool, _args):
        if tool == 'search_customer':
            return {'customers': [{'id': 'customer-1'}]}
        if tool == 'list_orders':
            return {'orders': [{'id': 'order-9', 'customer_id': 'customer-1'},
                               {'id': 'order-10', 'customer_id': 'customer-1'}]}
        raise AssertionError(tool)
    result = await find_order_by_customer_document(state=CommerceConversationState(), execute=execute,
        document_kind='cpf', document='11144477735')
    assert result.safety_reason == 'order_selection_required'
    assert 'Localizei mais de um pedido' in result.reply_text
    assert 'order-9' not in result.reply_text and 'order-10' not in result.reply_text


def test_effective_configuration_fingerprint_changes_when_policy_defaults_change():
    from app.configuration.runtime import configuration_fingerprint
    assert configuration_fingerprint({'values': {'maxQuestions': 3}}) != configuration_fingerprint(
        {'values': {'maxQuestions': 4}})
