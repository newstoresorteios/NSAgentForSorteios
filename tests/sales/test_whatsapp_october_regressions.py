from unittest.mock import AsyncMock

import pytest

from app.models import IncomingMessage, AgentResult, SalesInterpretation
from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
from app.sales.ready_delivery import try_ready_delivery
from app.ops.handoff_service import enrich_handoff_metadata
from app.verify.final_response import finalize_response
from app.catalog.specs.preference_normalize import _extract_budget_max


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['whatsapp', 'instagram'])
async def test_inventory_conversation_does_not_restart_qualification(channel, monkeypatch):
    token = set_persona_runtime(PersonaRuntimeConfig(loaded=True, enabled=True, workspace_id='test', tenant_id='newstore'))
    monkeypatch.setattr('app.sales.ready_delivery.enabled', lambda: True)
    lookup = AsyncMock(return_value={'success': True, 'complete': True, 'products': [
        {'name': 'Traska Venturer azul', 'url': 'https://www.newstorerj.com/venturer', 'listedAvailable': True}]})
    monkeypatch.setattr('app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery', lookup)
    state = CommerceConversationState()
    try:
        for text in ['Quero um relógio que esteja no estoque',
                     'Você consegue me passar quais modelos tem a pronta entrega?',
                     'Tenho um casamento para o próximo final de semana',
                     'Já falei que quero saber quais você tem em estoque', 'Está difícil entender?',
                     'Já falei que tenho um casamento', 'Logo é para mim', 'PQP']:
            incoming = IncomingMessage(text=text, channel=channel, sender_key='test', conversation_id='test')
            result = await try_ready_delivery(incoming, state)
            assert result is not None, text
            result = enrich_handoff_metadata(incoming, result, recent_turns=[])
            result, state = finalize_response(result, incoming=incoming, interpretation=None, previous_state=state)
            assert 'Traska Venturer azul' in result.reply_text, text
            assert not result.handoff_required
            assert not (result.commercial_data or {}).get('products')
            assert state.ready_delivery_context
        assert all(call.args[0] == 'pronta entrega' for call in lookup.call_args_list)
    finally:
        reset_persona_runtime(token)


@pytest.mark.parametrize('text', ['até 30 dias úteis', 'no máximo 40 mm', 'até 200 metros', 'menos de 2 semanas'])
def test_time_and_dimensions_are_not_budgets(text):
    assert _extract_budget_max(text) is None


def test_money_with_deadline_still_parses():
    assert _extract_budget_max('Até R$ 5000, entrega em 30 dias úteis') == 5000


@pytest.mark.parametrize('budget,expected', [(30, None), (5000, 5000)])
def test_duration_repair_preserves_a_real_prior_budget(budget, expected):
    from app.catalog.specs.preference_normalize import normalize_sales_interpretation
    interp = SalesInterpretation(domain='commerce', goal='inspect', preferences={'budget_max': budget},
        references_previous_context=True, needs_clarification=False, confidence=.99)
    normalized = normalize_sales_interpretation(interp, message_text='O prazo é de até 30 dias úteis?')
    assert normalized.preferences.budget_max == expected


def test_deadline_question_is_answered_without_price_or_poisoned_budget():
    from app.sales.inspection_copy import complete_inspection_copy
    incoming = IncomingMessage(channel='whatsapp', text='Não entendi. O prazo no site é de entrega ou envio?')
    interp = SalesInterpretation(goal='inspect', domain='commerce', subject={'reference': 'C032'}, preferences={'budget_max': 30},
                                 references_previous_context=True, needs_clarification=False, confidence=.99)
    result = AgentResult(reply_text='Não cabe no orçamento', commercial_data={'products': [
        {'id': '1', 'reference': 'C032', 'name': 'Certina', 'price': 7699, '_revalidated': True,
         'availability': 'Disponível em 30 dias úteis'}]}, response_metadata={'identity_inspection': True})
    result = complete_inspection_copy(result, interp, incoming.text)
    result, _ = finalize_response(result, incoming=incoming, interpretation=interp, previous_state=CommerceConversationState())
    assert 'disponibilidade' in result.reply_text
    assert 'orçamento' not in result.reply_text and 'R$' not in result.reply_text
    assert 'data exata de postagem' in result.reply_text


def test_overdue_order_does_not_become_shopping_or_payment_request():
    from app.sales.order_delivery_copy import complete_order_delivery_copy
    incoming = IncomingMessage(channel='whatsapp', text='Mas essa previsão já passou.')
    result = AgentResult(reply_text='Pagamento', commercial_data={'order_id': '1', 'status': 'A ENVIAR',
        'tracking': {'estimated_delivery_date': '2020-09-16'}})
    result = complete_order_delivery_copy(result, incoming.text)
    result = enrich_handoff_metadata(incoming, result, recent_turns=[])
    assert 'já passou' in result.reply_text and 'nova previsão' in result.reply_text
    assert 'pagamento' not in result.reply_text.lower()
    from app.sales.answer_council import check_pedido
    from app.sales.turn_contract import TurnContract
    assert 'handoff_on_live_cart' not in check_pedido(result, TurnContract(live_checkout=True)).issues


@pytest.mark.parametrize('text', ['Não tenho o número do pedido', 'Pode ser meu cpf?'])
def test_missing_order_number_offers_supported_lookup_identifier(text):
    from app.sales.service_intent_gate import service_intent_clarification
    result = service_intent_clarification(text, SalesInterpretation(domain='commerce', goal='after_sales', references_previous_context=True,
        needs_clarification=False, confidence=.99), CommerceConversationState())
    assert 'CPF' in result.reply_text and 'e-mail' in result.reply_text


@pytest.mark.parametrize('text', ['111.444.777-35', 'cliente@example.com'])
def test_supplied_identifier_reaches_existing_ownership_checked_lookup(text):
    from app.sales.service_intent_gate import service_intent_clarification
    interp = SalesInterpretation(domain='commerce', goal='after_sales', references_previous_context=True,
                                 needs_clarification=False, confidence=.99)
    assert service_intent_clarification(text, interp, CommerceConversationState()) is None
