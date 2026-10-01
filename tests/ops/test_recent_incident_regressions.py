"""Anonymized reproductions of the 2026-09-30 audit; no live tools/messages."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import AgentResult, IncomingMessage
from app.persona.persona_models import PersonaVersion
from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
from app.sales.after_sales_support import import_support_reply, try_import_support


@pytest.fixture
def persona():
    active = PersonaVersion(tenant_id='shop', workspace_id='shop', persona_key='sales',
                            version=1, name='Shop', instructions_hash='test', status='active',
                            instructions='A loja assume 100% dos trâmites e impostos. O cliente não paga nada a mais.')
    runtime = PersonaRuntimeConfig(loaded=True, enabled=True, workspace_id='shop', agent_display_name='Crono',
                                   greeting_text='Sou Crono, assistente virtual da Loja.').bind_sources(active_persona=active)
    token = set_persona_runtime(runtime)
    yield runtime
    reset_persona_runtime(token)


def test_import_policy_answers_who_pays_without_verifying_shipment(persona):
    msg = IncomingMessage(text='Sou eu quem paga isso?')
    result = try_import_support(msg, [{'role': 'user', 'content': 'A importação aguarda imposto na alfândega'}])
    assert result.intent == 'support'
    assert 'impostos de importação já estão incluídos' in result.reply_text
    assert 'verificar essa cobrança específica' in result.reply_text
    assert 'número do pedido' in result.reply_text
    assert not result.handoff_required


@pytest.mark.parametrize('policy', ['A loja não assume os impostos.', 'Os impostos não estão incluídos.',
                                   'O cliente paga os impostos.', ''])
def test_never_invents_tax_policy_for_other_store(persona, policy):
    persona.active_persona.instructions = policy
    result = import_support_reply()
    assert not result.response_metadata['published_tax_policy']
    assert 'já estão incluídos' not in result.reply_text


@pytest.mark.parametrize('field,value', [('status', 'draft'), ('workspace_id', 'other')])
def test_unpublished_or_foreign_persona_cannot_authorize_policy(persona, field, value):
    setattr(persona.active_persona, field, value)
    assert not import_support_reply().response_metadata['published_tax_policy']


@pytest.mark.asyncio
async def test_import_support_survives_double_check_without_llm(persona, monkeypatch):
    from app.verify import double_check
    from app.ops.handoff_service import enrich_handoff_metadata
    incoming = IncomingMessage(text='Tem uma importação aguardando pagamento de imposto na alfândega em Curitiba')
    result = try_import_support(incoming, [])
    judge = AsyncMock(side_effect=AssertionError('no paid calls'))
    monkeypatch.setattr(double_check, 'run_phase1_double_check', judge)
    result, report = await double_check.apply_double_check_async(incoming=incoming, result=result)
    result = enrich_handoff_metadata(incoming, result, recent_turns=[])
    judge.assert_not_called()
    assert not report.applied
    assert 'impostos de importação já estão incluídos' in result.reply_text


@pytest.mark.asyncio
async def test_import_screenshot_does_not_become_watch_catalog_failure(persona, monkeypatch):
    from app.agents import door
    from app.agents.door_media import try_media_routes
    from app.catalog.vision import image_product_id as vision
    from app.verify.catalog_delivery import enforce_photo_identity
    from app.ops.handoff_service import enrich_handoff_metadata
    identified = vision.ImageProductIdentification(is_watch=False, confidence=.99,
        visible_text=['Minhas Importações', 'Realizar pagamento', 'Aguardando pagamento em Curitiba'])
    monkeypatch.setattr(vision, 'identify_product_from_image', AsyncMock(return_value=identified))
    monkeypatch.setattr(vision, 'get_settings', lambda: SimpleNamespace(agent_image_search_enabled=True,
        agent_image_search_min_confidence=.55, agent_visual_search_enabled=False, database_url=''))
    monkeypatch.setattr(door, 'image_search_eligible', lambda _: True)
    incoming = IncomingMessage(channel='instagram', text='Veja o print', image_url='https://example.test/print.jpg', attachment_type='image')
    result = await try_media_routes(incoming, None)
    assert result.response_metadata['support_document']
    result = enforce_photo_identity(result)
    result = enrich_handoff_metadata(incoming, result, recent_turns=[])
    assert result.intent == 'support'
    assert 'Recebi o print' in result.reply_text
    assert 'catálogo' not in result.reply_text and 'relógio' not in result.reply_text


def test_greeting_introduces_crono_once(persona):
    from app.identity.agent_disclosure import apply_agent_disclosure
    result = AgentResult(reply_text='Olá! Eu sou o Crono, assistente virtual da Loja. Como posso ajudar?')
    result = apply_agent_disclosure(result, incoming=IncomingMessage(text='Boa tarde'), introduce=True)
    assert result.reply_text.count('Crono') == 1
    assert 'assistente virtual (IA)' in result.reply_text
    assert apply_agent_disclosure(result).reply_text == result.reply_text


@pytest.mark.parametrize('text,expected', [('Esse azul tá belíssimo.', True),
    ('Belíssimo, quanto custa?', False), ('Esse é lindíssimo!', True), ('Lindo, manda o link?', False)])
def test_story_compliment_never_triggers_catalog_but_questions_do(text, expected):
    from app.stories.instagram_story_intent import should_silence_story_message
    from app.stories.instagram_story_models import InstagramStoryContext
    incoming = IncomingMessage(text=text, instagram_story=InstagramStoryContext(replied_to_story=True))
    assert should_silence_story_message(incoming) is expected


def _unresolved_story():
    from app.stories.instagram_story_models import StoryResolutionResult
    from app.stories.instagram_story_service import story_result_to_agent_result
    resolution = StoryResolutionResult(tenant_id='shop', story_media_id='story', match_status='ambiguous',
        needs_clarification=True, followup_terms=['tissot', 'prx'],
        clarification_options=['relógio com mostrador preto', 'relógio com mostrador prateado', 'relógio com mostrador azul'],
        reply_hint='Qual você quer: mostrador preto, prateado ou azul?')
    incoming = IncomingMessage(channel='instagram', conversation_id='conversation', sender_key='customer', text='Valor')
    return incoming, story_result_to_agent_result(resolution, incoming=incoming)


def test_prx_size_followup_keeps_story_choices_instead_of_gold_lady_sku(persona):
    from app.stories.story_followup import unresolved_story_followup
    from app.ops.handoff_service import enrich_handoff_metadata
    incoming, first = _unresolved_story()
    incoming.text = 'PRX 35mm'
    state = SimpleNamespace(last_story_product=first.response_metadata['last_story_product'])
    result = unresolved_story_followup(incoming, state)
    assert result is not None
    result = enrich_handoff_metadata(incoming, result, recent_turns=[])
    assert 'prateado' in result.reply_text and 'azul' in result.reply_text
    assert not result.commercial_data and not result.handoff_required
    assert 'R$' not in result.reply_text and 'T931' not in result.reply_text
    incoming.text = 'O azul'
    state.last_story_product = result.response_metadata['last_story_product']
    selected = unresolved_story_followup(incoming, state)
    assert 'referência exata' in selected.reply_text
    assert 'prateado' not in selected.reply_text


def test_paulo_mido_followup_stays_bound_to_selected_story_region(persona):
    from app.stories.story_followup import unresolved_story_followup

    incoming = IncomingMessage(
        channel="instagram",
        conversation_id="ig:1439474121294747",
        sender_key="instagram:1439474121294747",
        text="Do mido que está no seu pulso",
    )
    ref = {
        "story_media_id": "18118620119283493",
        "tenant_id": "shop",
        "workspace_id": str(persona.workspace_id),
        "match_status": "ambiguous",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": incoming.conversation_id,
        "sender_key": incoming.sender_key,
        "followup_terms": ["mido", "baroncelli", "heritage", "traska"],
        "clarification_options": [],
        "story_regions": [
            {"position": "center", "label": "watch on wrist", "dial_color": "white",
             "strap_color": "brown", "brand_hypothesis": "Mido",
             "reference_hypothesis": "Baroncelli Heritage"},
            {"position": "left", "label": "white dial watch", "dial_color": "white",
             "brand_hypothesis": "Traska", "reference_hypothesis": "Commuter"},
        ],
    }
    result = unresolved_story_followup(incoming, SimpleNamespace(last_story_product=ref))
    assert result is not None
    assert "Mido Baroncelli Heritage" in result.reply_text
    assert result.response_metadata["last_story_product"]["selected_region_index"] == 0
    assert result.response_metadata["product_resolution_state"] == "unresolved"
    assert not result.commercial_data


@pytest.mark.parametrize('change', ['expired', 'workspace', 'conversation', 'new_product', 'other_brand_color', 'reference'])
def test_story_context_does_not_cross_boundaries_or_capture_new_search(persona, change):
    from app.stories.story_followup import unresolved_story_followup
    incoming, first = _unresolved_story()
    incoming.text = 'PRX 35mm'
    ref = first.response_metadata['last_story_product']
    if change == 'expired': ref['resolved_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    if change == 'workspace': ref['tenant_id'] = 'other'
    if change == 'conversation': incoming.conversation_id = 'other'
    if change == 'new_product': incoming.text = 'Agora quero outro Tissot PRX'
    if change == 'other_brand_color': incoming.text = 'Quanto custa o Seiko azul?'
    if change == 'reference': incoming.text = 'T137.210.11.041.00'
    assert unresolved_story_followup(incoming, SimpleNamespace(last_story_product=ref)) is None


def test_remove_vocative_only_not_business_mention():
    from app.verify.catalog_delivery import apply_output_style
    result = AgentResult(reply_text='Olá, cliente! Temos atendimento ao cliente.')
    assert apply_output_style(result).reply_text == 'Olá! Temos atendimento ao cliente.'


def test_three_distinct_story_watches_keep_their_colors():
    from app.stories.instagram_story_models import StoryVisualUnderstanding, VisualProductRegion
    from app.stories.instagram_story_service import _clarification_from_regions
    analysis = StoryVisualUnderstanding(watch_count=3, multiple_products=True,
        product_regions=[VisualProductRegion(dial_color=color) for color in ['black', 'silver', 'blue']])
    options, reply = _clarification_from_regions(analysis)
    assert len(options) == 3
    assert all(color in reply for color in ['preto', 'prateado', 'azul'])


@pytest.mark.asyncio
async def test_video_analysis_keeps_transport_type_and_one_call(monkeypatch):
    from app.stories import story_visual_analyzer as vision
    from app.stories.instagram_story_models import StoryVisualUnderstanding
    parse = AsyncMock(return_value=SimpleNamespace(parsed=StoryVisualUnderstanding(media_type='image'), metrics=None))
    monkeypatch.setattr(vision, 'parse_structured_output', parse)
    monkeypatch.setattr(vision, 'get_settings', lambda: SimpleNamespace(openai_model='test-only'))
    result = await vision.analyze_story_image(image_bytes=b'jpeg', extra_frame_bytes=[b'jpeg'] * 4, media_type='video')
    assert result.media_type == 'video'
    parse.assert_awaited_once()
    content = parse.call_args.kwargs['messages'][1]['content']
    assert len([item for item in content if item['type'] == 'image_url']) == 5


@pytest.mark.parametrize('env_name', ['SUPABASE_SERVICE_KEY', 'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_SECRET_KEY'])
def test_private_storage_accepts_standard_service_key_names(monkeypatch, env_name):
    from app.core.config import Settings
    for key in ['SUPABASE_SERVICE_KEY', 'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_SECRET_KEY']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(env_name, 'test-only-not-a-real-key')
    assert Settings(_env_file=None).supabase_service_key == 'test-only-not-a-real-key'


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['import', 'story'])
async def test_incident_reply_survives_full_output_pipeline(persona, monkeypatch, approved_critique, scenario):
    import app.message_pipeline as pipeline
    from app.commerce.commerce_context import CommerceConversationState
    from app.stories.story_followup import unresolved_story_followup
    monkeypatch.setattr(pipeline, 'get_settings', lambda: SimpleNamespace(audio_inbound_enabled=False, audio_outbound_enabled=False))
    monkeypatch.setattr(pipeline, 'load_commerce_conversation_state', lambda **kw: {})
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime', lambda **kw: persona)
    monkeypatch.setattr('app.configuration.workspace.resolve_conversation_workspace', lambda *a: None)
    if scenario == 'import':
        incoming = IncomingMessage(text='Sou eu quem paga isso?')
        draft = import_support_reply()
    else:
        incoming, draft = _unresolved_story()
    monkeypatch.setattr(pipeline, 'generate_agent_reply_async', AsyncMock(return_value=draft))
    result = await pipeline.process_incoming_message(incoming, {})
    assert not result.handoff_required
    if scenario == 'import':
        assert 'impostos de importação já estão incluídos' in result.reply_text
        assert 'catálogo' not in result.reply_text
    else:
        assert 'prateado' in result.reply_text
        state = CommerceConversationState.from_payload(result.response_metadata['commerce_state'])
        assert state.last_story_product['story_media_id'] == 'story'
        incoming.text = 'PRX 35mm'
        assert unresolved_story_followup(incoming, state) is not None
