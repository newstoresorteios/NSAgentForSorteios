"""Offline reproductions: three-watch Story -> PRX 35mm -> the blue one."""
from unittest.mock import AsyncMock

import pytest

from app.models import AgentResult, IncomingMessage
from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
from app.sales.ready_delivery import try_ready_delivery, enrich_story_ready_delivery
from app.stories.story_followup import unresolved_story_followup
from app.stories.instagram_story_models import StoryResolutionResult, StoryVisualUnderstanding
from app.stories.instagram_story_service import story_result_to_agent_result


@pytest.fixture
def runtime(monkeypatch):
    token = set_persona_runtime(PersonaRuntimeConfig(loaded=True, enabled=True, workspace_id='shop'))
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL', lambda: 'https://www.newstorerj.com/pronta-entrega')
    yield
    reset_persona_runtime(token)


@pytest.fixture
def lookup(monkeypatch):
    mock = AsyncMock(return_value={'success': True, 'complete': True, 'checkedAt': '2026-09-30T18:00:00Z',
        'products': [{'name': 'Tissot PRX Azul 35 mm', 'reference': 'T137.210.11.041.00',
                      'url': 'https://www.newstorerj.com/prx-azul', 'listedAvailable': True,
                      'product_id': 'foreign-id', 'price': 999}]})
    monkeypatch.setattr('app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery', mock)
    return mock


def incoming(text):
    return IncomingMessage(text=text, channel='instagram', sender_key='customer', conversation_id='thread')


def story_result():
    resolution = StoryResolutionResult(tenant_id='shop', story_media_id='story', match_status='ambiguous',
        needs_clarification=True, followup_terms=['prx'], catalog_query_base='tissot prx',
        clarification_options=['relógio com mostrador preto à esquerda',
                               'relógio com mostrador prateado no centro', 'relógio com mostrador azul à direita'],
        reply_hint='Qual relógio: preto, prateado ou azul?')
    return story_result_to_agent_result(resolution, incoming=incoming('Valor'))


@pytest.mark.asyncio
async def test_story_then_size_then_color_uses_com_without_inventing_identity(runtime, lookup):
    first = await enrich_story_ready_delivery(incoming('Valor'), story_result())
    lookup.assert_awaited_once_with('tissot prx')
    assert 'pronta entrega' in first.reply_text
    assert 'prateado' in first.reply_text
    assert '/prx-azul' not in first.reply_text  # Not selected yet.
    state = evolve_commerce_state(CommerceConversationState(), first)
    followup = unresolved_story_followup(incoming('PRX 35mm'), state)
    followup = await enrich_story_ready_delivery(incoming('PRX 35mm'), followup)
    assert lookup.call_args.args[0] == 'tissot prx 35mm'
    assert 'qual você quer' in followup.reply_text
    state = evolve_commerce_state(state, followup)
    selected = unresolved_story_followup(incoming('o azul'), state)
    selected = await enrich_story_ready_delivery(incoming('o azul'), selected)
    assert lookup.call_args.args[0] == 'tissot prx 35mm azul'
    assert 'Encontrei esta opção' in selected.reply_text
    assert 'Ainda não confirmei' in selected.reply_text
    assert 'A foto do anúncio' in selected.reply_text
    assert 'T931' not in selected.reply_text and 'R$' not in selected.reply_text
    assert not selected.commercial_data and not selected.handoff_required
    assert 'foreign-id' not in str(selected.response_metadata)
    assert not evolve_commerce_state(state, selected).active_product
    state = evolve_commerce_state(state, selected)
    price = unresolved_story_followup(incoming('qual o valor?'), state)
    assert price is not None
    await enrich_story_ready_delivery(incoming('qual o valor?'), price)
    assert lookup.call_args.args[0] == 'tissot prx 35mm azul'


@pytest.mark.asyncio
async def test_door_media_applies_storefront_lookup_to_followup(runtime, lookup):
    from app.agents.door_media import try_media_routes
    state = evolve_commerce_state(CommerceConversationState(), story_result())
    result = await try_media_routes(incoming('PRX 35mm'), state)
    assert result.response_metadata['ready_delivery_check']['complete']
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_story_route_also_queries_com(runtime, lookup, monkeypatch):
    from app.agents.door_media import try_media_routes
    from app.stories.instagram_story_models import InstagramStoryContext
    resolution = StoryResolutionResult(tenant_id='shop', story_media_id='story', match_status='ambiguous',
        needs_clarification=True, followup_terms=['prx'], catalog_query_base='tissot prx 35mm',
        clarification_options=['relógio com mostrador azul', 'relógio com mostrador preto'], reply_hint='Qual relógio?')
    monkeypatch.setattr('app.stories.instagram_story_intent.should_route_story_question', lambda m: True)
    monkeypatch.setattr('app.stories.instagram_story_intent.story_requires_text_first', lambda m: False)
    monkeypatch.setattr('app.stories.instagram_story_service.resolve_story_product_question', AsyncMock(return_value=resolution))
    msg = incoming('Valor')
    msg.instagram_story = InstagramStoryContext(story_media_id='story', replied_to_story=True)
    result = await try_media_routes(msg, CommerceConversationState())
    assert result.response_metadata['ready_delivery_check']['complete']
    lookup.assert_awaited_once_with('tissot prx 35mm')


@pytest.mark.asyncio
async def test_confirmed_matches_are_not_replaced(runtime, lookup):
    result = AgentResult(reply_text='Modelo já confirmado no catálogo.', response_metadata={
        'last_story_product': {'match_status': 'matched', 'catalog_query': 'Tissot PRX'}})
    assert await enrich_story_ready_delivery(incoming('Valor'), result) is result
    assert result.reply_text == 'Modelo já confirmado no catálogo.'
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_delivery_preference_survives_short_replies(runtime, lookup):
    first = await try_ready_delivery(incoming('Tem PRX 35mm a pronta entrega?'))
    state = evolve_commerce_state(CommerceConversationState(), first)
    again = await try_ready_delivery(incoming('o azul'), state)
    assert 'prx 35mm' in lookup.call_args.args[0].lower()
    assert 'azul' in lookup.call_args.args[0]
    assert again.response_metadata['catalog_source'].endswith('.com/pronta-entrega')
    assert again.response_metadata['ready_delivery_check']['stock_confirmed'] is False
    assert not again.commercial_data
    state = evolve_commerce_state(state, again)
    assert await try_ready_delivery(incoming('manda o link'), state) is not None
    assert 'azul' in lookup.call_args.args[0]


@pytest.mark.asyncio
async def test_model_answer_after_generic_ready_delivery_question(runtime, lookup):
    lookup.return_value = {'success': True, 'complete': True, 'requiresModel': True}
    first = await try_ready_delivery(incoming('Tem pronta entrega?'))
    state = evolve_commerce_state(CommerceConversationState(), first)
    await try_ready_delivery(incoming('PRX 35mm'), state)
    assert lookup.call_args.args[0] == 'prx 35mm'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('tenant_id','elsewhere'), ('conversation_id','elsewhere'),
    ('sender_key','elsewhere'), ('channel','whatsapp'), ('created_at','2000-01-01T00:00:00+00:00'),
    ('created_at','bad'), ('created_at','2030-01-01T00:00:00+00:00')])
async def test_ready_context_is_scoped_and_expires(runtime, lookup, field, value):
    first = await try_ready_delivery(incoming('PRX 35mm pronta entrega'))
    state = evolve_commerce_state(CommerceConversationState(), first)
    state.ready_delivery_context[field] = value
    lookup.reset_mock()
    assert await try_ready_delivery(incoming('o azul'), state) is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['meu pedido de pronta entrega', 'Já comprei, pronta entrega',
    'quero um atendente', 'rastrear pedido', 'não quero pronta entrega', 'quero sob encomenda',
    'Bom dia', 'Obrigado', 'Agora quero outro Seiko azul', 'Quanto custa o Seiko azul?'])
async def test_other_routes_are_not_captured(runtime, lookup, text):
    first = await try_ready_delivery(incoming('Tissot PRX 35mm pronta entrega'))
    state = evolve_commerce_state(CommerceConversationState(), first)
    lookup.reset_mock()
    assert await try_ready_delivery(incoming(text), state) is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_failure_does_not_mean_out_of_stock(runtime, lookup):
    from app.tray.tray_adapter_client import TrayAdapterError
    lookup.side_effect = TrayAdapterError('unavailable')
    result = await enrich_story_ready_delivery(incoming('Valor'), story_result())
    assert 'Não consegui consultar' in result.reply_text
    assert 'não significa' in result.reply_text
    assert result.response_metadata['ready_delivery_check']['complete'] is False
    assert not result.commercial_data and not result.handoff_required


@pytest.mark.asyncio
async def test_other_workspace_store_and_confirmed_story_unchanged(runtime, lookup, monkeypatch):
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL', lambda: 'https://other.example/stock')
    result = story_result()
    original = result.reply_text
    assert (await enrich_story_ready_delivery(incoming('Valor'), result)).reply_text == original
    assert await try_ready_delivery(incoming('PRX pronta entrega')) is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_response_and_restart_clear_context_not_order(runtime, lookup):
    from app.sales.dialogue_phase import reset_browse_memory_keep_orders
    state = CommerceConversationState(order_id='1234', pending_action='awaiting_payment')
    first = await try_ready_delivery(incoming('PRX pronta entrega'))
    state = evolve_commerce_state(state, first)
    assert state.order_id == '1234' and state.pending_action == 'awaiting_payment'
    assert reset_browse_memory_keep_orders(state).ready_delivery_context is None
    other = AgentResult(reply_text='Outra pesquisa', response_metadata={'domain': 'commerce'})
    assert evolve_commerce_state(state, other).ready_delivery_context is None


def test_visual_hints_are_bounded_and_do_not_merge_different_models():
    from app.stories.story_catalog_context import catalog_hints, refine_story_reference
    analysis = StoryVisualUnderstanding(visible_brands=['Tissot'], collection_hypotheses=['PRX'],
                                       visible_text=['PRX em 35mm'])
    assert catalog_hints(analysis) == 'tissot prx 35mm'
    analysis.visible_brands.append('Seiko')
    assert not catalog_hints(analysis)
    ref = story_result().response_metadata['last_story_product']
    assert refine_story_reference(ref, 'à direita')['selected_option'].endswith('à direita')
    assert refine_story_reference(ref, 'o verde')['selected_option'] is None
    assert 'prateado' in refine_story_reference(ref, 'o prata')['selected_option']
    assert not refine_story_reference(ref, '35mm ou 40mm')['catalog_query']
    mixed = {**ref, 'catalog_query_base': '', 'followup_terms': ['prx', 'seiko']}
    assert not refine_story_reference(mixed, 'o azul')['catalog_query']
    assert refine_story_reference(mixed, 'PRX 35mm')['catalog_query'] == 'prx 35mm'


@pytest.mark.asyncio
async def test_story_enrichment_survives_output_pipeline(runtime, lookup, monkeypatch, approved_critique):
    from types import SimpleNamespace
    import app.message_pipeline as pipeline
    from app.persona.persona_runtime import get_persona_runtime
    from app.ops.turn_runtime import TurnRuntimeContext
    turn = TurnRuntimeContext(trace_id='ready-test', conversation_key='test')
    monkeypatch.setattr(pipeline, 'get_current_turn', lambda: turn)
    state = evolve_commerce_state(CommerceConversationState(), story_result())
    chosen = unresolved_story_followup(incoming('PRX 35mm azul'), state)
    chosen = await enrich_story_ready_delivery(incoming('PRX 35mm azul'), chosen)
    monkeypatch.setattr(pipeline, 'get_settings', lambda: SimpleNamespace(audio_inbound_enabled=False, audio_outbound_enabled=False))
    monkeypatch.setattr(pipeline, 'load_commerce_conversation_state', lambda **kw: {})
    active_runtime = get_persona_runtime()
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime', lambda **kw: active_runtime)
    monkeypatch.setattr('app.configuration.workspace.resolve_conversation_workspace', lambda *a: None)
    monkeypatch.setattr(pipeline, 'generate_agent_reply_async', AsyncMock(return_value=chosen))
    result = await pipeline.process_incoming_message(incoming('PRX 35mm azul'), {})
    assert 'www.newstorerj.com/prx-azul' in result.reply_text
    assert 'Ainda não confirmei' in result.reply_text
    assert not result.handoff_required
    assert result.response_metadata['ready_delivery_check']['stock_confirmed'] is False
    assert turn.outbound_snapshot['ready_delivery_check']['complete'] is True
    assert turn.outbound_snapshot['ready_delivery_check']['source'].endswith('.com/pronta-entrega')
    assert 'query' not in turn.outbound_snapshot['ready_delivery_check']
