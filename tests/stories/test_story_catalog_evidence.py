import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.catalog.index.repository import CatalogIndexRepository
from app.stories import story_product_matcher as matcher
from app.stories.story_catalog_evidence import match_scene_catalog
from app.stories.instagram_story_models import StoryProductCandidate, StoryVisualUnderstanding, VisualProductRegion


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
