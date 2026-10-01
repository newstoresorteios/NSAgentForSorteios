"""Regression for luizkriv: mixed Story -> Traska -> Aço -> branco."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.models import IncomingMessage
from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
from app.stories.story_highlight_references import StoryHighlightReference


@pytest.fixture
def setup(monkeypatch):
    token = set_persona_runtime(PersonaRuntimeConfig(
        loaded=True, enabled=True, workspace_id='shop', tenant_id='newstore'))
    monkeypatch.setattr('app.persona.site_knowledge.STORE_PRONTA_ENTREGA_URL',
                        lambda: 'https://www.newstorerj.com/pronta-entrega')
    calls = []

    def reference(query, **kw):
        assert kw['tenant_id'] == 'newstore'
        calls.append('references')
        return None

    async def catalog(*args):
        calls.append('catalog')
        return {'products': []}

    async def storefront(query):
        calls.append('site')
        return {'success': True, 'complete': True, 'products': [
            {'name': 'Traska Venturer GMT Preto 4217', 'listedAvailable': True,
             'url': 'https://www.newstorerj.com/relogios/traska-preto-4217'},
            {'name': 'Traska Venturer GMT Branco 4217', 'listedAvailable': True,
             'url': 'https://www.newstorerj.com/relogios/traska-branco-4217'},
        ]}

    monkeypatch.setattr('app.stories.story_highlight_references.current_workspace_reference', reference)
    monkeypatch.setattr('app.agents.door.execute_tool', AsyncMock(side_effect=catalog))
    monkeypatch.setattr('app.tray.tray_adapter_client.TrayAdapterClient.search_ready_delivery',
                        AsyncMock(side_effect=storefront))
    yield calls
    reset_persona_runtime(token)


def incoming(text):
    return IncomingMessage(text=text, channel='instagram', sender_key='luizkriv', conversation_id='thread')


def state():
    return CommerceConversationState(last_story_product={
        'story_media_id': 'story', 'match_status': 'ambiguous',
        'workspace_id': 'shop', 'tenant_id': 'newstore',
        'conversation_id': 'thread', 'sender_key': 'luizkriv',
        'resolved_at': datetime.now(timezone.utc).isoformat(),
        'catalog_query': '', 'catalog_query_base': '',
        'followup_terms': ['mido', 'traska', 'gmt', 'aco', 'branco', 'preto', 'azul'],
        'clarification_options': ['product:12918', 'product:13474', 'product:13462'],
        'story_regions': [
            {'brand_hypothesis': 'Mido', 'reference_hypothesis': 'Baroncelli Heritage',
             'dial_color': 'branco', 'label': 'Mido branco'},
            *[{'brand_hypothesis': 'Traska', 'reference_hypothesis': 'GMT automático',
               'dial_color': color, 'label': 'Traska ' + color} for color in ('branco', 'preto', 'azul')],
        ],
    })


@pytest.mark.asyncio
async def test_traska_then_steel_lists_options_without_claiming_story_identity(setup):
    from app.agents.door_media import try_media_routes
    current = state()
    for text in ('Traska', 'Aço'):
        result = await try_media_routes(incoming(text), current)
        assert setup[-3:] == ['references', 'catalog', 'site']
        assert 'traska-preto-4217' in result.reply_text
        assert 'traska-branco-4217' in result.reply_text
        assert 'Qual dessas opções' in result.reply_text
        assert 'print' not in result.reply_text
        assert 'Ainda não confirmei' in result.reply_text
        assert not result.commercial_data
        current = evolve_commerce_state(current, result)
        assert current.active_product is None
        assert current.last_story_product['catalog_query'] == 'traska'
    result = await try_media_routes(incoming('branco'), current)
    ref = result.response_metadata['last_story_product']
    assert ref['selected_region_index'] == 1
    assert ref['catalog_query'] == 'traska gmt automatico branco'
    assert ref['match_status'] == 'ambiguous'


@pytest.mark.asyncio
async def test_configured_reference_wins_before_any_catalog_or_site_search(setup, monkeypatch):
    from app.agents.door_media import try_media_routes

    def reference(*a, **kw):
        setup.append('references')
        return StoryHighlightReference(7, 'Traska Venturer GMT Branco 4217',
            'https://www.newstorerj.com/relogios/traska-branco-4217', ('traska',))

    monkeypatch.setattr('app.stories.story_highlight_references.current_workspace_reference', reference)
    result = await try_media_routes(incoming('Traska'), state())
    assert setup == ['references']
    assert 'nas referências dos Stories' in result.reply_text
    assert 'traska-branco-4217' in result.reply_text
    assert not result.commercial_data
    assert result.response_metadata['story_catalog_check']['reference_id'] == 7


@pytest.mark.asyncio
async def test_live_catalog_options_used_when_no_configured_reference(setup, monkeypatch):
    from app.agents.door_media import try_media_routes

    async def catalog(*a):
        setup.append('catalog')
        return {'products': [
            {'id': '1', 'name': 'Traska Venturer GMT', 'available': True,
             'url': 'https://www.newstorerj.com.br/relogios/traska-venturer'},
            {'id': '2', 'name': 'Mido Baroncelli', 'available': True,
             'url': 'https://www.newstorerj.com.br/relogios/mido'},
        ]}

    monkeypatch.setattr('app.agents.door.execute_tool', catalog)
    result = await try_media_routes(incoming('Traska'), state())
    assert setup == ['references', 'catalog']
    assert 'traska-venturer' in result.reply_text and 'Mido' not in result.reply_text
    assert not result.commercial_data
    assert evolve_commerce_state(state(), result).active_product is None


@pytest.mark.asyncio
async def test_unavailable_catalog_and_catalog_failure_fall_back_to_site(setup, monkeypatch):
    from app.agents.door_media import try_media_routes
    catalog = AsyncMock(return_value={'products': [{'id': '13474', 'name': 'Traska Branco',
        'available': False, 'url': 'https://www.newstorerj.com.br/traska-branco'}]})
    monkeypatch.setattr('app.agents.door.execute_tool', catalog)
    result = await try_media_routes(incoming('Traska'), state())
    assert 'traska-branco-4217' in result.reply_text
    catalog.side_effect = RuntimeError('offline')
    result = await try_media_routes(incoming('Traska'), state())
    assert 'traska-branco-4217' in result.reply_text
    assert result.response_metadata['story_catalog_check']['catalog_failed']


@pytest.mark.asyncio
async def test_reference_cannot_override_customer_color(setup, monkeypatch):
    from app.agents.door_media import try_media_routes
    monkeypatch.setattr('app.stories.story_highlight_references.current_workspace_reference',
        lambda *a, **kw: StoryHighlightReference(7, 'Traska Venturer Branco',
            'https://www.newstorerj.com/relogios/traska-branco', ('traska',)))
    result = await try_media_routes(incoming('Traska preto'), state())
    assert result.response_metadata['story_catalog_check']['reference_id'] is None
    assert 'catalog' in setup and 'site' in setup


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('workspace_id', 'other'), ('sender_key', 'other'),
    ('conversation_id', 'other'), ('tenant_id', 'other')])
async def test_foreign_story_does_not_search_references_or_catalog(setup, field, value):
    from app.stories.story_followup import unresolved_story_followup
    current = state()
    current.last_story_product[field] = value
    assert unresolved_story_followup(incoming('Traska'), current) is None
    assert setup == []


@pytest.mark.asyncio
@pytest.mark.parametrize('use_reference', [False, True])
async def test_options_survive_final_pipeline(setup, monkeypatch, approved_critique, use_reference):
    from types import SimpleNamespace
    import app.message_pipeline as pipeline
    from app.agents.door_media import try_media_routes
    from app.persona.persona_runtime import get_persona_runtime
    if use_reference:
        monkeypatch.setattr('app.stories.story_highlight_references.current_workspace_reference',
            lambda *a, **kw: StoryHighlightReference(7, 'Traska Venturer GMT Branco 4217',
                'https://www.newstorerj.com/relogios/traska-branco-4217', ('traska',)))
    chosen = await try_media_routes(incoming('Traska'), state())
    monkeypatch.setattr(pipeline, 'get_settings', lambda: SimpleNamespace(
        audio_inbound_enabled=False, audio_outbound_enabled=False))
    monkeypatch.setattr(pipeline, 'load_commerce_conversation_state', lambda **kw: {})
    runtime = get_persona_runtime()
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime', lambda **kw: runtime)
    monkeypatch.setattr('app.configuration.workspace.resolve_conversation_workspace', lambda *a: None)
    monkeypatch.setattr(pipeline, 'generate_agent_reply_async', AsyncMock(return_value=chosen))
    result = await pipeline.process_incoming_message(incoming('Traska'), {})
    assert 'traska-branco-4217' in result.reply_text
    assert not result.response_metadata.get('factual_fallback_active')
    assert not (result.commercial_data or {}).get('products')
