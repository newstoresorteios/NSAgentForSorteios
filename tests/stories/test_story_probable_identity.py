from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import IncomingMessage
from app.stories.instagram_story_models import StoryVisualUnderstanding, StoryQuestionType
from app.stories.instagram_story_service import _finalize_story_catalog_match, story_result_to_agent_result
from app.stories.story_identity_verifier import IdentityReview, approved_checks
from app.stories.story_probable_identity import probable_identity


def incident():
    """Anonymized shape of the blue Venturer incident; no customer/media data."""
    analysis = StoryVisualUnderstanding(media_type='video', watch_count=1, frames_analyzed=8,
        image_quality='usable', ambiguity_reasons=['A marca não está legível no relógio mostrado.'],
        model_hypotheses=['Traska Venturer GMT'],
        product_regions=[dict(position='center', dial_color='azul escuro',
            brand_hypothesis='Traska', reference_hypothesis='Venturer GMT', frame_indexes=list(range(1, 8)))])
    evidence = {'candidates': [dict(product_id=pid, catalog_item_key='newstore:' + pid, score=.195, source='tray_search')
                               for pid in ['13450', '13462', '13474', '13488']],
        'approved_identities': [], 'identity_reviews': [dict(product_id=pid, region_index=0,
            verdict='consistent' if pid == '13450' else 'different',
            matches_catalog_photo=pid == '13450', visual_support_frame_indexes=[3, 5, 7] if pid == '13450' else [],
            distinguishing_features=['Dark blue dial', 'Yellow GMT hand', 'Steel bracelet', 'Date at six'],
            conflicts=[] if pid == '13450' else ['Different dial and GMT hand'])
            for pid in ['13450', '13462', '13474', '13488']]}
    product = dict(id='13450', name='Relógio Traska Venturer GMT Automático Azul 4216', price=9999, stock=10)
    return analysis, evidence, product


@pytest.mark.asyncio
async def test_probable_survives_conversion_and_failure_explanation_without_commercial_binding(monkeypatch):
    from app.ops.failure_explanation import apply_failure_explanation
    from app.sales.ready_delivery import enrich_story_ready_delivery
    from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime
    analysis, evidence, product = incident()
    repo = MagicMock()
    result = await _finalize_story_catalog_match(repo=repo, tenant='newstore', provider='meta', account='test',
        media_id='test', analysis=analysis, question_type=StoryQuestionType.GENERIC, shadow_only=False,
        metrics={}, execute_tool=AsyncMock(return_value=product), worker_evidence=evidence, customer_text='Qual é esse no seu braço?')
    assert not result.resolved and result.match_status == 'ambiguous'
    assert result.product_id is None and result.product_payload is None
    repo.confirm_match.assert_not_called()
    assert approved_checks(IdentityReview(checks=evidence['identity_reviews']), products={'13450': product},
                           analysis=analysis, frame_count=8) == []
    token = set_persona_runtime(PersonaRuntimeConfig(loaded=True, enabled=True, tenant_id='newstore', workspace_id='test'))
    try:
        # Explicit color selection used to overwrite the identification reply.
        incoming = IncomingMessage(text='Qual é o azul?', channel='instagram', sender_key='customer', conversation_id='thread')
        agent = story_result_to_agent_result(result, incoming=incoming)
        monkeypatch.setattr('app.sales.ready_delivery.enabled', lambda: True)
        monkeypatch.setattr('app.sales.ready_delivery.lookup', AsyncMock(return_value={
            'complete': True, 'products': [{'name': 'Outra opção', 'url': 'https://www.newstorerj.com/example'}]}))
        agent = await enrich_story_ready_delivery(incoming, agent)
        agent.reply_text = 'Resposta genérica substituída por uma revisão.'
        agent = apply_failure_explanation(agent)
        from app.verify.final_response import finalize_response
        from app.commerce.commerce_context import CommerceConversationState
        agent.reply_text = 'É exatamente esse relógio. Custa R$ 9999 e temos 10 em estoque.'
        agent, state = finalize_response(agent, incoming=incoming, interpretation=None,
                                          previous_state=CommerceConversationState())
        assert 'parece ser o Traska Venturer GMT, de mostrador azul' in agent.reply_text
        assert '9999' not in agent.reply_text
        assert not (agent.commercial_data or {}).get('products')
        assert state.active_product is None and not state.pending_action_product_ids
        assert 'active_product' not in agent.response_metadata
        assert agent.response_metadata['product_resolution_state'] == 'unresolved'
        assert agent.response_metadata['story_probable_identity']['commercial_authority'] is False
    finally:
        reset_persona_runtime(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['poor', 'multi', 'uncertain', 'missing', 'duplicate', 'two_matches',
    'conflict', 'unexplained_difference', 'few_frames', 'invalid_frame', 'weak_features', 'wrong_id', 'wrong_model', 'wrong_color', 'api_error'])
async def test_unsafe_hypotheses_remain_unresolved(failure):
    analysis, evidence, product = incident()
    check = evidence['identity_reviews'][0]
    if failure == 'poor': analysis.image_quality = 'poor'
    if failure == 'multi': analysis.watch_count = 2
    if failure == 'uncertain': evidence['identity_reviews'][1]['verdict'] = 'uncertain'
    if failure == 'missing': evidence['identity_reviews'].pop()
    if failure == 'duplicate': evidence['identity_reviews'].append(check.copy())
    if failure == 'two_matches': evidence['identity_reviews'][1]['verdict'] = 'consistent'
    if failure == 'conflict': check['conflicts'] = ['Wrong hand']
    if failure == 'unexplained_difference': evidence['identity_reviews'][1]['conflicts'] = []
    if failure == 'few_frames': check['visual_support_frame_indexes'] = [3, 3]
    if failure == 'invalid_frame': check['visual_support_frame_indexes'] = [3, 99]
    if failure == 'weak_features': check['distinguishing_features'] = ['blue', 'blue']
    if failure == 'wrong_id': product['id'] = '999'
    if failure == 'wrong_model': product['name'] = 'Traska Summiteer Azul'
    if failure == 'wrong_color': product['name'] = 'Traska Venturer GMT Preto'
    tool = AsyncMock(return_value=product, side_effect=RuntimeError('unavailable') if failure == 'api_error' else None)
    assert await probable_identity(analysis=analysis, worker_evidence=evidence, execute_tool=tool) is None
