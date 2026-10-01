"""Real frame input, explicitly pre-annotated vision output; no remote model calls."""
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from pydantic import SecretStr

from app.core.config import Settings
from app.stories.instagram_story_models import StoryVisualUnderstanding, StoryProductCandidate
from app.stories import story_analysis_worker as worker, story_analysis_jobs as jobs
from app.stories.instagram_story_media import StoryMediaError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/story_media'
CASE = json.loads((FIXTURES / 'annotations.json').read_text(encoding='utf-8'))['cases'][0]
FRAME = FIXTURES / CASE['fixture_frame']


def test_real_frame_provenance_and_annotation_do_not_claim_exact_identity():
    assert hashlib.sha256(FRAME.read_bytes()).hexdigest() == CASE['fixture_sha256']
    with Image.open(FRAME) as image:
        assert image.size == (720, 1280)
    from app.stories.story_identity_verifier import IdentityReview, IdentityCheck, approved_checks
    analysis = StoryVisualUnderstanding.model_validate(CASE['analysis'])
    review = IdentityReview(checks=[IdentityCheck(product_id='fixture-product', region_index=0,
        verdict='consistent', identifiers_read_on_watch=['H13519711'], supporting_frame_indexes=[0, 1])])
    assert approved_checks(review, products={'fixture-product': {'reference': 'H13519711'}},
                           analysis=analysis, frame_count=7) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['file_too_large', 'decoder_unavailable'])
async def test_real_frame_reaches_vision_when_video_falls_back_without_audio(monkeypatch, failure):
    from app.stories import story_visual_analyzer as vision
    from app.stories.story_video_audio import StoryAudioEvidence
    settings = Settings(_env_file=None)
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(vision, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime',
                        lambda **kw: SimpleNamespace(configuration_bundle={}))
    job = {'id': 1, 'workspace_id': 'fixture-w', 'tenant_id': 'fixture-t', 'provider': 'meta',
           'instagram_account_id': 'fixture-a', 'story_media_id': 'fixture-s', 'media_storage_path': None,
           'generation': 'fixture-g', 'lease_owner': 'fixture-o', 'attempts': 1}
    monkeypatch.setattr(jobs, 'source_media', lambda job: {'id': 1, 'channel_metadata': {}})
    thumbnail = SimpleNamespace(path=FRAME, content_type='image/jpeg', sha256=CASE['fixture_sha256'],
                                byte_count=FRAME.stat().st_size, close=Mock())
    # Original transport is unavailable in this scenario; only the actual frame
    # serving as thumbnail is inspected. No synthetic bytes are passed to vision.
    original = SimpleNamespace(path=FRAME, content_type='video/mp4', sha256=CASE['source_sha256'],
                               byte_count=CASE['source_bytes'], close=Mock())
    first = StoryMediaError('file_too_large') if failure == 'file_too_large' else original
    monkeypatch.setattr(worker, 'download_story_media_file', AsyncMock(side_effect=[first, thumbnail]))
    async def hydrate(story):
        return story.model_copy(update={'story_thumbnail_url_private': SecretStr('https://cdninstagram.com/fixture.jpg')})
    monkeypatch.setattr('app.stories.instagram_story_service._hydrate_story_media', hydrate)
    monkeypatch.setattr(worker, 'extract_video_frames_best_effort', lambda *a, **kw: [])
    monkeypatch.setattr(worker, 'SupabasePrivateStoryMediaStorage',
                        lambda **kw: SimpleNamespace(put_private=AsyncMock(return_value=None)))
    monkeypatch.setattr('app.ops.instagram_media_archive._save', Mock())
    monkeypatch.setattr('app.stories.story_video_audio.transcribe_story_video',
                        AsyncMock(return_value=StoryAudioEvidence('unavailable')))
    captured = []
    async def annotated_vision(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(parsed=StoryVisualUnderstanding.model_validate(CASE['analysis']), metrics=None)
    monkeypatch.setattr(vision, 'parse_structured_output', annotated_vision)
    candidate = StoryProductCandidate(catalog_item_key='fixture:hamilton', product_id='fixture-product')
    monkeypatch.setattr('app.stories.story_catalog_evidence.match_scene_catalog',
                        AsyncMock(return_value=([candidate], {})))
    monkeypatch.setattr('app.stories.story_identity_verifier.verify_identities', AsyncMock(return_value=([], [])))
    finish = Mock(return_value=True)
    monkeypatch.setattr(jobs, 'finish_job', finish)
    monkeypatch.setattr(jobs, 'redis_command', AsyncMock())
    assert await worker.analyze_job(job)
    image_part = next(p for p in captured[0]['messages'][1]['content'] if p.get('type') == 'image_url')
    actual = base64.b64decode(image_part['image_url']['url'].split(',', 1)[1])
    assert hashlib.sha256(actual).hexdigest() == CASE['fixture_sha256']
    output = finish.call_args.kwargs['result']
    assert output['schema'] == jobs.EVIDENCE_VERSION
    assert output['analysis']['media_type'] == 'video' and output['analysis']['audio_status'] == 'unavailable'
    assert output['media']['video_frame_fallback_reason'] == failure
    assert output['approved_identities'] == []


@pytest.mark.asyncio
async def test_hypothesis_never_supplies_a_price_and_confirmed_identity_uses_live_adapter(monkeypatch):
    from app.stories.instagram_story_service import _revalidate_product
    # Catalog values are deliberately different on the second read. A Story's
    # historical advertisement and identity evidence cannot freeze price/stock.
    execute = AsyncMock(side_effect=[{'id': 'fixture-product', 'price': 5000, 'stock': 1, 'available': True},
                                     {'id': 'fixture-product', 'price': 5500, 'stock': 0, 'available': False}])
    async def keep_url(product):
        return product
    monkeypatch.setattr('app.catalog.media.product_media.ensure_product_has_live_url', keep_url)
    first, failed1, _ = await _revalidate_product(product_id='fixture-product', execute_tool=execute)
    second, failed2, _ = await _revalidate_product(product_id='fixture-product', execute_tool=execute)
    assert not failed1 and not failed2
    assert (first['price'], first['stock']) == (5000, 1)
    assert (second['price'], second['stock']) == (5500, 0)
    assert second['_factual_source'] == 'tray_live'
    assert execute.await_count == 2
    assert 'price' not in CASE['analysis'] and CASE['expected']['exact_identity_confirmed'] is False
