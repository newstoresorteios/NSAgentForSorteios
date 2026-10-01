"""Exact Story clarification must also find the separate ready-stock storefront."""
from unittest.mock import AsyncMock

import pytest

from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import IncomingMessage, SalesInterpretation
from app.persona.persona_runtime import PersonaRuntimeConfig, reset_persona_runtime, set_persona_runtime
from app.sales.ready_delivery import try_ready_delivery, try_reference_ready_delivery


URL = 'https://www.newstorerj.com/relogios/relogios-hamilton/relogio-hamilton-american-classic-boulton-mechanical-h13519711'
TEXT = 'Hamilton american Classic boulton H13519711'


def message(text=TEXT):
    return IncomingMessage(text=text, channel='instagram', conversation_id='thread', sender_key='customer')


def interpretation(**changes):
    values = dict(domain='commerce', goal='find', subject={
        'reference': 'H13519711', 'brand': 'Hamilton', 'model': 'American Classic Boulton'},
        information_needed=['price'], references_previous_context=True,
        needs_clarification=False, confidence=1)
    return SalesInterpretation(**{**values, **changes})


@pytest.fixture
def setup(monkeypatch):
    runtime = PersonaRuntimeConfig(loaded=True, enabled=True, tenant_id='newstore',
                                   workspace_id='shop', prefer_ready_stock=True)
    token = set_persona_runtime(runtime)
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL',
                        lambda: 'https://www.newstorerj.com/pronta-entrega')
    lookup = AsyncMock(return_value={'success': True, 'complete': True, 'products': [{
        'name': 'Relógio Hamilton American Classic Boulton Mechanical H13519711',
        'reference': 'H13519711', 'url': URL, 'listedAvailable': True,
        'id': 'foreign-store-id', 'price': 9199.99, 'stock': 1,
    }]})
    monkeypatch.setattr('app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery', lookup)
    yield runtime, lookup
    reset_persona_runtime(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('goal,needed', [('find', ['catalog']), ('inspect', ['price']), ('inspect', ['catalog'])])
async def test_explicit_identity_searches_ready_stock_without_inferred_story_color(setup, goal, needed):
    _, lookup = setup
    result = await try_reference_ready_delivery(message(), interpretation(goal=goal, information_needed=needed))
    lookup.assert_awaited_once_with('H13519711')
    assert URL in result.reply_text and 'pronta entrega' in result.reply_text
    assert '30 dias' not in result.reply_text and 'foto' not in result.reply_text.lower()
    assert result.response_metadata['ready_delivery_exact_reference'] == 'H13519711'
    assert not result.commercial_data
    assert 'foreign-store-id' not in str(result.response_metadata)
    assert '9199' not in str(result.response_metadata)
    assert result.response_metadata['ready_delivery_check']['stock_confirmed'] is False


@pytest.mark.asyncio
async def test_catalog_flow_prefers_exact_ready_listing_before_other_store_catalog(setup, monkeypatch):
    from app.sales.catalog_retrieve import retrieve_catalog_or_clarify

    discovery = AsyncMock(side_effect=AssertionError('Ready listing should answer first'))
    monkeypatch.setattr('app.sales.adaptive_discovery.prepare_discovery', discovery)
    result = await retrieve_catalog_or_clarify(
        message=message(), facts={}, customer_context={}, interpretation=interpretation(),
        plan={'intent': 'product_search'}, state=CommerceConversationState(),
        recent_turns=[], resolved_product=None,
    )
    assert result.response_metadata['response_source'] == 'ready_delivery_storefront'
    assert URL in result.reply_text
    discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_ready_listing_passes_sales_orchestration_and_url_validation(setup, monkeypatch):
    import app.sales_agent as sales
    from app.verify.factual_validator import build_fact_pack

    fallback = AsyncMock(side_effect=AssertionError('Do not substitute the other store listing'))
    monkeypatch.setattr(sales, '_execute_compiled_product_retrieval', fallback)
    result = await sales.handle_sales_message(
        message(), {}, {}, interpretation(), commerce_state=CommerceConversationState(),
    )
    assert URL in result.reply_text
    assert result.response_metadata['response_source'] == 'ready_delivery_storefront'
    pack = build_fact_pack(result)
    assert URL in pack.trusted_urls
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_followup_remains_in_ready_store_and_does_not_create_foreign_product(setup):
    from app.agents.door_media import try_media_routes
    from datetime import datetime, timezone

    result = await try_reference_ready_delivery(message(), interpretation())
    previous = CommerceConversationState(last_story_product={
        'tenant_id': 'newstore', 'workspace_id': 'shop', 'conversation_id': 'thread',
        'sender_key': 'customer', 'match_status': 'ambiguous',
        'resolved_at': datetime.now(timezone.utc).isoformat(),
        'catalog_query': 'hamilton american classic boulton branco',
        'selected_option': 'Hamilton Boulton', 'followup_terms': ['hamilton', 'boulton'],
    })
    state = evolve_commerce_state(previous, result)
    assert state.active_product is None
    assert await try_media_routes(message('qual o preço?'), state) is None
    reply = await try_ready_delivery(message('qual o preço?'), state)
    assert URL in reply.reply_text and not reply.commercial_data
    assert state.ready_delivery_context['query'] == 'H13519711'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['empty', 'failure', 'wrong_reference', 'unavailable'])
async def test_unconfirmed_ready_result_allows_normal_catalog_search(setup, outcome):
    _, lookup = setup
    if outcome == 'empty':
        lookup.return_value['products'] = []
    elif outcome == 'failure':
        from app.tray.tray_adapter_client import TrayAdapterError
        lookup.side_effect = TrayAdapterError('unavailable')
    elif outcome == 'wrong_reference':
        lookup.return_value['products'][0]['reference'] = 'H13431553'
    else:
        lookup.return_value['products'][0]['listedAvailable'] = False
    assert await try_reference_ready_delivery(message(), interpretation()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('text,changes', [
    ('H13519711 tem safira?', {'goal': 'inspect', 'information_needed': ['catalog']}),
    ('H13519711 preço e calibre?', {'goal': 'inspect', 'information_needed': ['catalog', 'price']}),
    ('Meu pedido H13519711', {'goal': 'after_sales'}),
    ('Quero H13519711 sob encomenda', {}),
    ('Não quero pronta entrega do H13519711', {}),
    ('Seminovo H13519711', {}),
    ('Comprar H13519711', {'goal': 'buy', 'purchase_action': 'create_cart'}),
    ('Quero H13519711 e H13431553', {}),
    ('H13519711', {'subject': {'reference': 'H13431553'}}),
])
async def test_technical_questions_other_intents_and_conflicting_identity_are_preserved(setup, text, changes):
    _, lookup = setup
    assert await try_reference_ready_delivery(message(text), interpretation(**changes)) is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_preference_and_store_boundary_are_required(setup, monkeypatch):
    runtime, lookup = setup
    runtime.prefer_ready_stock = False
    assert await try_reference_ready_delivery(message(), interpretation()) is None
    runtime.prefer_ready_stock = True
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL', lambda: 'https://other.example/stock')
    assert await try_reference_ready_delivery(message(), interpretation()) is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_persona_preserves_normal_catalog(monkeypatch):
    monkeypatch.setattr('app.persona.persona_runtime.get_persona_runtime', lambda: None)
    assert await try_reference_ready_delivery(message(), interpretation()) is None


@pytest.mark.asyncio
async def test_review_cannot_replace_ready_listing_with_other_store_price(setup, monkeypatch):
    import json
    from types import SimpleNamespace
    from app.verify import response_critique as critique

    result = await try_reference_ready_delivery(message(), interpretation())
    other_store_facts = {'search_products': {'products': [{
        'id': '2243', 'name': TEXT, 'reference': 'H13519711', 'price': 8899.99,
        'availability': 'Disponível em 30 dias úteis',
        'url': 'https://www.newstorerj.com.br/relogios/hamilton-h13519711',
    }]}}
    assert critique.apply_search_products_to_result(result=result, api_facts=other_store_facts) is result
    monkeypatch.setattr(critique, 'get_settings', lambda: SimpleNamespace(openai_api_key='test', openai_model='test'))

    async def compose(**kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        assert payload['api_facts'] == {}
        assert payload['commercial_data'] == {}
        assert payload['ready_delivery_evidence']['products'][0]['url'] == URL
        assert not payload['search_products_empty']
        assert 'Não use preço, prazo, estoque ou IDs da loja .com.br' in kwargs['messages'][0]['content']
        return SimpleNamespace(text=result.reply_text)

    monkeypatch.setattr('app.llm.openai_gateway.generate_text_output', compose)
    regenerated = await critique._regenerate_reply(
        incoming=message(), result=result, verdict=critique.CritiqueVerdict(pass_check=False),
        api_facts=other_store_facts, recent_turns=[], commerce_state=None,
    )
    assert regenerated is not None and URL in regenerated.reply_text
    assert not regenerated.commercial_data
