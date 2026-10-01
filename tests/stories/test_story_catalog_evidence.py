import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.catalog.index.repository import CatalogIndexRepository
from app.stories import story_product_matcher as matcher
from app.stories.story_catalog_evidence import match_scene_catalog
from app.stories.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding, VisualProductRegion


@pytest.mark.parametrize('text,index', [('o Mido', 0), ('Traska branco', 1), ('o branco', None),
                                     ('Traska', None), ('o preto', 2), ('Mido ou Traska', None)])
def test_customer_can_select_brand_without_inheriting_another_watch(text, index):
    from app.stories.story_selection import selected_region
    scene = StoryVisualUnderstanding(product_regions=[
        VisualProductRegion(brand_hypothesis='Mido', dial_color='branco'),
        VisualProductRegion(brand_hypothesis='Traska', dial_color='branco'),
        VisualProductRegion(brand_hypothesis='Traska', dial_color='preto')])
    selected = selected_region(scene, text)
    assert selected == (scene.product_regions[index] if index is not None else None)


@pytest.mark.asyncio
async def test_searches_each_watch_without_crossing_brands_audio_or_identifiers(monkeypatch):
    scene = StoryVisualUnderstanding(watch_count=3, multiple_products=True,
        visible_brands=['Mido', 'Traska'], visible_references=['M027.207.11.010.00'],
        audio_transcript='Outra cor de caixa e pulseira. Traska.',
        product_regions=[VisualProductRegion(brand_hypothesis='Mido', reference_hypothesis='Baroncelli', dial_color='branco'),
                         VisualProductRegion(brand_hypothesis='Traska', dial_color='branco'),
                         VisualProductRegion(brand_hypothesis='Traska', dial_color='preto')])
    observed = []
    async def search(**kw):
        scoped = kw['analysis']
        observed.append(scoped)
        assert kw['identity_search'] and kw['tenant_id'] == 'tenant'
        await kw['execute_tool']('search_products', {'brand': scoped.visible_brands[0], 'tokens': ['GMT']})
        pid = '1' if scoped.visible_brands == ['Mido'] else '2' if scoped.dial_colors == ['branco'] else '3'
        return [StoryProductCandidate(catalog_item_key=f'tray:{pid}', product_id=pid)]
    monkeypatch.setattr(matcher, 'match_story_to_catalog', search)
    tool = AsyncMock(return_value={'products': []})
    merged, regions = await match_scene_catalog(tenant_id='tenant', analysis=scene, execute_tool=tool)
    assert {c.product_id for c in merged} == {'1', '2', '3'}
    assert [regions[str(i)][0]['product_id'] for i in range(3)] == ['1', '2', '3']
    assert tool.await_count == 2  # White/black Traska share identical catalog requests.
    assert all(a.watch_count == 1 and not a.audio_transcript and not a.visible_references for a in observed)
    assert observed[1].collection_hypotheses == []
    assert scene.audio_transcript and scene.visible_references  # Original evidence is intact.


@pytest.mark.asyncio
async def test_failed_region_cancels_other_searches_and_shared_requests(monkeypatch):
    active, closed = asyncio.Event(), asyncio.Event()
    async def tool(*args):
        active.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async def search(**kw):
        if kw['analysis'].visible_brands == ['Mido']:
            await active.wait()
            raise RuntimeError('catalog unavailable')
        await kw['execute_tool']('search_products', {'brand': 'Traska'})
    monkeypatch.setattr(matcher, 'match_story_to_catalog', search)
    scene = StoryVisualUnderstanding(product_regions=[VisualProductRegion(brand_hypothesis='Mido'),
                                                      VisualProductRegion(brand_hypothesis='Traska')])
    with pytest.raises(RuntimeError):
        await match_scene_catalog(tenant_id='tenant', analysis=scene, execute_tool=tool)
    assert closed.is_set()


def test_identity_lookup_is_tenant_scoped_and_includes_inactive_or_sold_out_watches(monkeypatch):
    fetch = Mock(return_value=[{'product_id': '3', 'available': False, 'stock': 0}])
    monkeypatch.setattr(CatalogIndexRepository, '_fetch', fetch)
    rows = CatalogIndexRepository().search_identity_by_brand(tenant_id='tenant', brand='Traska', limit=500)
    assert rows[0]['available'] is False
    sql, params = fetch.call_args.args
    assert 'tenant_id = %(tenant_id)s' in sql and 'lower(brand) = lower(%(brand)s)' in sql
    assert 'stock' not in sql and 'available' not in sql
    assert params == {'tenant_id': 'tenant', 'brand': 'Traska', 'limit': 100}


@pytest.mark.asyncio
async def test_inactive_traska_remains_identity_candidate_without_reindexing_stale_data(monkeypatch):
    monkeypatch.setattr(CatalogIndexRepository, 'search_exact', lambda *a, **kw: [])
    monkeypatch.setattr(CatalogIndexRepository, 'search_lexical', lambda *a, **kw: [])
    monkeypatch.setattr(CatalogIndexRepository, 'search_identity_by_brand', lambda *a, **kw: [
        {'tenant_id': 'tenant', 'product_id': '13474', 'brand': 'Traska', 'reference': '4215',
         'title_normalized': 'Relógio Traska Venturer GMT branco', 'available': False, 'stock': 0}])
    monkeypatch.setattr('app.catalog.vision.product_image_index.visual_search_from_caption', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.catalog.media.storefront_search.search_storefront', AsyncMock(return_value=[]))
    reindex = Mock()
    monkeypatch.setattr('app.catalog.index.catalog_index.index_products_best_effort', reindex)
    analysis = StoryVisualUnderstanding(watch_count=1, visible_brands=['Traska'],
        dial_colors=['branco'], visible_text=['TRASKA', 'GMT'], mechanisms_suggested=['GMT'])
    result = await matcher.match_story_to_catalog(tenant_id='tenant', analysis=analysis,
        execute_tool=AsyncMock(return_value={'products': []}), identity_search=True)
    assert [c.product_id for c in result] == ['13474']
    assert result[0].source == 'identity_index'
    assert not any(r.startswith(('sku:', 'reference:', 'ean:')) for r in result[0].match_reasons)
    reindex.assert_not_called()


@pytest.mark.asyncio
async def test_selection_uses_its_region_even_when_another_brand_has_the_same_color(monkeypatch):
    from app.stories import instagram_story_service as service
    from app.stories.instagram_story_models import StoryQuestionType
    scene = StoryVisualUnderstanding(watch_count=2, multiple_products=True,
        product_regions=[VisualProductRegion(position='left', brand_hypothesis='Mido', dial_color='branco'),
                         VisualProductRegion(position='right', brand_hypothesis='Traska', dial_color='branco')])
    candidates = [StoryProductCandidate(catalog_item_key=f'tray:{i}', product_id=str(i), score=1,
        match_reasons=['listing:relógio branco']).model_dump() for i in (1, 2)]
    shared = {'candidates': candidates, 'region_candidates': {'0': [candidates[0]], '1': [candidates[1]]},
              'approved_identities': []}
    repo = SimpleNamespace(save_candidates=Mock(), mark_ambiguous=Mock(), confirm_match=Mock())
    result = await service._finalize_story_catalog_match(repo=repo, tenant='tenant', provider='meta',
        account='a', media_id='s', analysis=scene, question_type=StoryQuestionType.PRICE,
        shadow_only=False, metrics={}, execute_tool=AsyncMock(), worker_evidence=shared, customer_text='o da direita')
    assert [c.product_id for c in result.candidates] == ['2']
    assert not result.resolved
    repo.confirm_match.assert_not_called()


@pytest.mark.asyncio
async def test_independent_review_cannot_move_candidates_or_frames_between_watches(monkeypatch):
    from app.stories import story_identity_verifier as verifier
    scene = StoryVisualUnderstanding(watch_count=2, multiple_products=True, media_type='video',
        product_regions=[VisualProductRegion(dial_color='branco', strap_color='marrom', frame_indexes=[1]),
                         VisualProductRegion(dial_color='azul', strap_color='prateado', frame_indexes=[2, 5, 7])])
    candidates = [StoryProductCandidate(catalog_item_key=f'tray:{i}', product_id=str(i)) for i in (1, 2)]
    regions = {str(i): [c.model_dump()] for i, c in enumerate(candidates)}
    calls = []
    async def parse(**kw):
        assert kw['text_format'] is verifier.RegionIdentityReview
        parts = kw['messages'][1]['content']
        texts = [p['text'] for p in parts if p['type'] == 'text']
        body = ' '.join(texts)
        assert 'PRIVATE-REF' not in body
        is_first = 'product_id=1' in body
        expected = ['Frame original 1'] if is_first else ['Frame original 2', 'Frame original 5', 'Frame original 7']
        assert [t for t in texts if t.startswith('Frame original')] == expected
        assert not ('product_id=1' in body and 'product_id=2' in body)
        calls.append(body)
        pid, allowed_frame = ('1', 1) if is_first else ('2', 5)
        return SimpleNamespace(parsed=verifier.RegionIdentityReview(checks=[
            verifier.RegionIdentityCheck(product_id=pid, verdict='uncertain', visual_support_frame_indexes=[allowed_frame]),
            verifier.RegionIdentityCheck(product_id='2' if is_first else '1', verdict='consistent'),
            verifier.RegionIdentityCheck(product_id=pid, verdict='consistent', supporting_frame_indexes=[0])]))
    async def tool(name, args):
        return {'id': args['product_id'], 'reference': 'PRIVATE-REF',
                'primary_image_url': 'https://images.tcdn.com.br/' + args['product_id'] + '.jpg'}
    monkeypatch.setattr(verifier, 'parse_structured_output', parse)
    approved, reviews = await verifier.verify_identities(analysis=scene, frames=[b'frame'] * 8,
        candidates=candidates, execute_tool=tool, region_candidates=regions)
    assert len(calls) == 2 and not approved
    assert [(r['product_id'], r['region_index']) for r in reviews] == [('1', 0), ('2', 1)]
