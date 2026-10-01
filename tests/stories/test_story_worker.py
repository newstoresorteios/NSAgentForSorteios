import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.config import Settings
from app.stories import instagram_story_media as media
from app.stories import story_analysis_jobs as jobs
from app.stories import story_analysis_worker as worker
from app.stories.instagram_story_models import StoryVisualUnderstanding, VisualProductRegion, StoryProductCandidate
from app.stories.story_identity_verifier import IdentityReview, IdentityCheck, approved_checks


class VideoStream(httpx.AsyncByteStream):
    def __init__(self, size, cancel=False):
        self.size, self.cancel = size, cancel

    async def __aiter__(self):
        yield b'\x00\x00\x00\x18ftypmp42'
        left = self.size - 12
        while left:
            if self.cancel:
                raise asyncio.CancelledError()
            count = min(left, 1024 * 1024)
            yield bytes(count)
            left -= count


@pytest.mark.asyncio
@pytest.mark.parametrize('with_length', [True, False])
@pytest.mark.parametrize('size,accepted', [(104_857_600, True), (104_857_601, False)])
async def test_100_mib_stream_boundary_and_cleanup(monkeypatch, tmp_path, with_length, size, accepted):
    monkeypatch.setattr(media, 'get_settings', lambda: Settings(_env_file=None))
    monkeypatch.setattr(media, 'validate_story_media_url', lambda url: (url, ['157.240.0.1']))
    original_temp = media.tempfile.NamedTemporaryFile
    monkeypatch.setattr(media.tempfile, 'NamedTemporaryFile', lambda **kw: original_temp(dir=tmp_path, **kw))
    headers = {'content-type': 'video/mp4'}
    if with_length:
        headers['content-length'] = str(size)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=VideoStream(size)))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: original_client(transport=transport, **kw))
    if accepted:
        result = await media.download_story_media_file('https://cdninstagram.com/a')
        assert result.path.stat().st_size == result.byte_count == size
        assert result.content_type == 'video/mp4'
        result.close()
    else:
        with pytest.raises(media.StoryMediaError, match='file_too_large'):
            await media.download_story_media_file('https://cdninstagram.com/a')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_download_removes_partial_file(monkeypatch, tmp_path):
    original_temp = media.tempfile.NamedTemporaryFile
    monkeypatch.setattr(media.tempfile, 'NamedTemporaryFile', lambda **kw: original_temp(dir=tmp_path, **kw))
    monkeypatch.setattr(media, 'get_settings', lambda: Settings(_env_file=None))
    monkeypatch.setattr(media, 'validate_story_media_url', lambda url: (url, []))
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=VideoStream(1024, cancel=True)))
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: original_client(transport=transport, **kw))
    with pytest.raises(asyncio.CancelledError):
        await media.download_story_media_file('https://cdninstagram.com/a')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_private_storage_scope_cannot_be_changed(monkeypatch):
    monkeypatch.setattr(media, 'get_settings', lambda: Settings(_env_file=None))
    with pytest.raises(media.StoryMediaError, match='storage_scope_invalid'):
        await media.download_story_media_file('', workspace_id='owner',
            private_path='supabase://conversation-media/private/instagram-stories/other/' + 'a' * 48)


@pytest.mark.asyncio
async def test_free_storage_limit_does_not_upload_an_oversized_original(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, SUPABASE_URL='https://project.supabase.co', SUPABASE_SERVICE_KEY='test-placeholder')
    monkeypatch.setattr(media, 'get_settings', lambda: settings)
    path = tmp_path / 'large.mp4'
    with path.open('wb') as stream:
        stream.truncate(60 * 1024 * 1024)
    methods = []
    def respond(request):
        methods.append(request.method)
        assert request.method == 'GET'
        return httpx.Response(200, json={'public': False, 'file_size_limit': 50 * 1024 * 1024})
    original = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    storage = media.SupabasePrivateStoryMediaStorage(bucket='conversation-media')
    assert await storage.put_private(content=path, content_type='video/mp4', sha256='a'*64, tenant_id='w') is None
    assert storage.last_error == 'storage_object_too_large'
    assert methods == ['GET'] and path.exists()


def evidence():
    analysis = StoryVisualUnderstanding(media_type='video', watch_count=1, product_identity_confidence=1,
        visible_references=['AB-12345'], product_regions=[VisualProductRegion(
            visible_text=['AB-12345'], frame_indexes=[0, 1])])
    review = IdentityReview(checks=[IdentityCheck(product_id='42', region_index=0, verdict='consistent',
        identifiers_read_on_watch=['AB12345'], supporting_frame_indexes=[0, 1])])
    return analysis, review, {'42': {'reference': 'AB-12345'}}


def test_independent_exact_reference_can_be_shared():
    analysis, review, products = evidence()
    assert approved_checks(review, products=products, analysis=analysis, frame_count=2)[0]['product_id'] == '42'


@pytest.mark.parametrize('problem', ['only_confidence', 'overlay_only', 'one_frame', 'invented_frame',
                                    'conflict', 'wrong_reference', 'unseen_product', 'duplicate_variant', 'wrong_region', 'audio_conflict'])
def test_unsafe_identities_cannot_poison_shared_cache(problem):
    analysis, review, products = evidence()
    check = review.checks[0]
    if problem == 'only_confidence': check.identifiers_read_on_watch = []
    if problem == 'overlay_only':
        analysis.product_regions[0].visible_text = []
        analysis.visible_references = []
    if problem == 'one_frame': check.supporting_frame_indexes = [0, 0]
    if problem == 'invented_frame': check.supporting_frame_indexes = [0, 200]
    if problem == 'conflict': check.conflicts = ['dial_color_mismatch']
    if problem == 'wrong_reference': products['42']['reference'] = 'AB-99999'
    if problem == 'unseen_product': check.product_id = '666'
    if problem == 'duplicate_variant': products['43'] = {'reference': 'AB-12345'}
    if problem == 'wrong_region': check.region_index = 9
    if problem == 'audio_conflict': analysis.ambiguity_reasons = ['audio_model_conflict']
    assert approved_checks(review, products=products, analysis=analysis, frame_count=2) == []


def test_cache_keys_isolate_scope_versions_and_manual_corrections():
    job = dict(workspace_id='w', tenant_id='t', provider='meta', instagram_account_id='a',
               story_media_id='s', analysis_version='v', generation='g')
    original = jobs.cache_key(job)
    for field in job:
        changed = {**job, field: job[field] + '-new'}
        assert jobs.cache_key(changed) != original


@pytest.mark.parametrize('problem', [None, 'similar_name', 'negated', 'two_watches', 'no_photo', 'one_visual_frame', 'generic_features'])
def test_spoken_reference_requires_catalog_photo_and_distinct_visual_evidence(problem):
    analysis, review, products = evidence()
    analysis.visible_references = []
    analysis.product_regions[0].visible_text = []
    analysis.audio_status = 'transcribed'
    analysis.audio_transcript = 'Este relógio é o AB 12345.'
    check = review.checks[0]
    check.identifiers_read_on_watch = []
    check.supporting_frame_indexes = []
    check.visual_support_frame_indexes = [0, 1]
    check.matches_catalog_photo = check.audio_refers_to_visible_watch = True
    check.distinguishing_features = ['ponteiros vazados', 'bezel dodecagonal', 'numerais aplicados']
    products['42']['primary_image_url'] = 'https://images.tcdn.com.br/watch.jpg'
    if problem == 'similar_name': analysis.audio_transcript = 'É da coleção AB.'
    if problem == 'negated': analysis.audio_transcript = 'Não é o AB 12345.'
    if problem == 'two_watches': analysis.watch_count = 2
    if problem == 'no_photo': products['42']['primary_image_url'] = ''
    if problem == 'one_visual_frame': check.visual_support_frame_indexes = [0, 0]
    if problem == 'generic_features': check.distinguishing_features = ['relógio', 'relógio', '']
    approved = approved_checks(review, products=products, analysis=analysis, frame_count=2)
    assert bool(approved) is (problem is None)
    if approved:
        assert approved[0]['method'] == 'spoken_reference_visual_corroboration'


@pytest.mark.asyncio
async def test_redis_outage_and_corrupt_json_are_cache_misses(monkeypatch):
    import sys
    class Broken:
        async def __aenter__(self): raise ConnectionError('private endpoint secret')
        async def __aexit__(self, *args): pass
    monkeypatch.setenv('STORY_REDIS_URL', 'redis://test')
    monkeypatch.setitem(sys.modules, 'redis.asyncio', SimpleNamespace(from_url=lambda *a, **kw: Broken()))
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(asyncio=sys.modules['redis.asyncio']))
    log = Mock()
    monkeypatch.setattr(jobs, 'log_event', log)
    assert await jobs.redis_command('get', {}) is None
    assert 'secret' not in str(log.call_args)


@pytest.mark.asyncio
async def test_unverified_video_cannot_be_promoted_by_tie_breaker(monkeypatch):
    from app.stories import instagram_story_service as service
    from app.stories.instagram_story_models import StoryQuestionType
    candidate = StoryProductCandidate(catalog_item_key='tray:42', product_id='42', score=1,
                                     match_reasons=['reference:AB-12345'])
    analysis, _, _ = evidence()
    execute = AsyncMock()
    repo = SimpleNamespace(save_candidates=Mock(), mark_ambiguous=Mock(), confirm_match=Mock())
    tie = Mock(return_value=candidate)
    monkeypatch.setattr('app.stories.story_match_decider.try_resolve_tied_candidates', tie)
    result = await service._finalize_story_catalog_match(repo=repo, tenant='t', provider='meta',
        account='a', media_id='s', analysis=analysis, question_type=StoryQuestionType.PRICE,
        shadow_only=False, metrics={}, execute_tool=execute,
        worker_evidence={'candidates': [candidate.model_dump()], 'approved_identities': []})
    assert not result.resolved and result.match_status == 'ambiguous'
    assert result.product_payload is None
    repo.confirm_match.assert_not_called()
    execute.assert_not_called()
    tie.assert_not_called()


@pytest.mark.asyncio
async def test_a_verified_watch_is_live_revalidated_for_each_customer(monkeypatch):
    from app.stories import instagram_story_service as service
    from app.stories.instagram_story_models import StoryQuestionType
    candidate = StoryProductCandidate(catalog_item_key='tray:42', product_id='42', score=1,
                                     match_reasons=['reference:AB-12345'])
    analysis, _, _ = evidence()
    repo = SimpleNamespace(save_candidates=Mock(), confirm_match=Mock())
    revalidate = AsyncMock(side_effect=[({'id': '42', 'name': 'Watch', 'price': 100}, False, None, []),
                                       ({'id': '42', 'name': 'Watch', 'price': 200}, False, None, [])])
    monkeypatch.setattr(service, '_revalidate_matched_story_product', revalidate)
    shared = {'candidates': [candidate.model_dump()], 'approved_identities': [{'product_id': '42', 'region_index': 0}]}
    results = [await service._finalize_story_catalog_match(repo=repo, tenant='t', provider='meta',
        account='a', media_id='s', analysis=analysis, question_type=StoryQuestionType.PRICE,
        shadow_only=False, metrics={}, execute_tool=AsyncMock(), worker_evidence=shared) for _ in range(2)]
    assert [r.product_payload['price'] for r in results] == [100, 200]
    assert 'price' not in shared


@pytest.mark.asyncio
async def test_new_customer_cannot_inherit_another_customers_watch(monkeypatch):
    from app.stories import instagram_story_service as service
    from app.stories.instagram_story_models import StoryQuestionType
    scene = StoryVisualUnderstanding(media_type='video', watch_count=2, multiple_products=True,
        product_regions=[VisualProductRegion(position='left', dial_color='azul'),
                         VisualProductRegion(position='right', dial_color='preto')])
    candidates = [StoryProductCandidate(catalog_item_key=f'tray:{i}', product_id=str(i), score=1,
        match_reasons=[f'listing:Relógio {color}']) for i, color in [(42, 'azul'), (43, 'preto')]]
    shared = {'candidates': [c.model_dump() for c in candidates], 'approved_identities':
              [{'product_id': '42', 'region_index': 0}, {'product_id': '43', 'region_index': 1}]}
    repo = SimpleNamespace(save_candidates=Mock(), mark_ambiguous=Mock(), confirm_match=Mock())
    async def revalidate(**kw):
        return {'id': kw['product_id'], 'name': 'Watch'}, False, None, []
    monkeypatch.setattr(service, '_revalidate_matched_story_product', revalidate)
    output = []
    for text in ['o azul', 'o preto', 'valor?']:
        output.append(await service._finalize_story_catalog_match(repo=repo, tenant='t', provider='meta',
            account='a', media_id='s', analysis=scene, question_type=StoryQuestionType.PRICE,
            shadow_only=False, metrics={}, execute_tool=AsyncMock(), worker_evidence=shared, customer_text=text))
    assert [r.product_id for r in output] == ['42', '43', None]
    assert not output[-1].resolved
    repo.confirm_match.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_defers_then_reuses_evidence_without_leaking_context(monkeypatch):
    from app.models import IncomingMessage
    from app.stories.instagram_story_models import InstagramStoryContext
    from app.stories.story_tenant import TenantResolution
    settings = Settings(_env_file=None, DATABASE_URL='test')
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.stories.instagram_story_service.story_rollout_allows', lambda **kw: (True, 'full'))
    monkeypatch.setattr('app.stories.instagram_story_intent.should_route_story_question', lambda incoming: True)
    monkeypatch.setattr('app.stories.story_tenant.resolve_story_tenant', AsyncMock(return_value=TenantResolution(ok=True, tenant_id='t', source='instagram_account_map')))
    monkeypatch.setattr('app.stories.story_product_repository.StoryProductRepository.get_by_story', lambda *a, **kw: None)
    job = {'id': 1, 'status': 'pending'}
    monkeypatch.setattr(jobs, 'ensure_job', lambda **kw: job.copy())
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, 'read_result', read)
    incoming = IncomingMessage(channel='instagram', text='valor?', raw={'inbound_id': 1, 'inbox_id': 1},
        instagram_story=InstagramStoryContext(story_media_id='s', provider='meta', instagram_account_id='a'))
    async def pending():
        with pytest.raises(worker.StoryAnalysisPending) as exc:
            await worker.prepare_story_turn(incoming, 'workspace')
        assert exc.value.job_id == 1 and worker.current_story_evidence() is None
    await asyncio.gather(*(pending() for _ in range(100)))
    job['status'] = 'ready'
    read.return_value = {'schema': jobs.EVIDENCE_VERSION, 'analysis': {'watch_count': 1}}
    token = await worker.prepare_story_turn(incoming, 'workspace')
    assert worker.current_story_evidence()['result']['analysis']['watch_count'] == 1
    worker.reset_story_evidence(token)
    assert worker.current_story_evidence() is None


@pytest.mark.asyncio
async def test_inbox_waits_without_outbound_or_fake_failure(monkeypatch):
    from app.ingress import worker as inbox
    from app.models import IncomingMessage
    incoming = IncomingMessage(channel='instagram', conversation_id='ig:test', text='valor?')
    monkeypatch.setattr(inbox, 'incoming_from_inbox_payload', lambda _: incoming)
    monkeypatch.setattr(inbox, 'claim_inbound_message', lambda _: (True, 940))
    monkeypatch.setattr(inbox, 'is_caption_echo_of_recent_image', lambda _: False)
    monkeypatch.setattr(inbox, 'attach_recent_image_for_followup', lambda value: value)
    monkeypatch.setattr(inbox, 'has_successful_agent_response', lambda _: False)
    monkeypatch.setattr('app.ops.human_takeover.human_takeover_active', lambda _: False)
    monkeypatch.setattr('app.ingress.outbox.has_sent_outbound', lambda _: False)
    monkeypatch.setattr('app.ingress.outbox.get_accepted_outbound', lambda _: None)
    monkeypatch.setattr(inbox, '_customer_context_for', AsyncMock(return_value={}))
    monkeypatch.setattr('app.configuration.workspace.stamp_silent_inbound_workspace', lambda *args: None)
    monkeypatch.setattr('app.message_pipeline.process_incoming_message', AsyncMock(side_effect=worker.StoryAnalysisPending(5)))
    deferred, outbound = Mock(), Mock()
    monkeypatch.setattr(jobs, 'defer_inbox', deferred)
    monkeypatch.setattr(inbox, 'enqueue_accepted_outbound', outbound)
    result = await inbox.process_inbox_row({'id': 385, 'payload_json': {}}, lock_held=True)
    assert result['deferred'] == 'story_analysis'
    deferred.assert_called_once_with([385], 5)
    outbound.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('original_stored', [True, False])
async def test_worker_combines_frames_audio_catalog_and_publishes_only_once(monkeypatch, tmp_path, original_stored):
    from app.stories.story_video_audio import StoryAudioEvidence
    settings = Settings(_env_file=None)
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime', lambda **kw: SimpleNamespace(configuration_bundle={}))
    job = {'id': 7, 'workspace_id': 'w', 'tenant_id': 't', 'provider': 'meta',
           'instagram_account_id': 'a', 'story_media_id': 's', 'media_storage_path': 'private-path' if original_stored else None,
           'generation': 'g', 'lease_owner': 'o', 'attempts': 1}
    monkeypatch.setattr(jobs, 'source_media', lambda job: {'id': 1, 'workspace_id': 'w', 'channel_metadata': {}})
    download = AsyncMock(return_value=SimpleNamespace(path=tmp_path/'original', content_type='video/mp4',
        sha256='a'*64, byte_count=20_000_000, close=Mock()))
    monkeypatch.setattr(worker, 'download_story_media_file', download)
    async def put(**kw):
        return 'private-frame' if kw['content_type'] == 'image/jpeg' else None
    storage = SimpleNamespace(put_private=AsyncMock(side_effect=put))
    monkeypatch.setattr(worker, 'SupabasePrivateStoryMediaStorage', lambda **kw: storage)
    monkeypatch.setattr('app.ops.instagram_media_archive._save', Mock())
    def decode(path, **kw):
        kw['frame_times'].extend([1.0, 55.0])
        return [b'first-frame', b'last-frame']
    monkeypatch.setattr(worker, 'extract_video_frames_best_effort', decode)
    monkeypatch.setattr('app.stories.story_video_audio.transcribe_story_video', AsyncMock(return_value=StoryAudioEvidence('transcribed', 'relógio azul')))
    analysis, _, _ = evidence()
    visual = AsyncMock(return_value=analysis)
    monkeypatch.setattr('app.stories.story_visual_analyzer.analyze_story_image', visual)
    candidate = StoryProductCandidate(catalog_item_key='tray:42', product_id='42', score=1)
    monkeypatch.setattr('app.stories.story_product_matcher.match_story_to_catalog', AsyncMock(return_value=[candidate]))
    monkeypatch.setattr('app.stories.story_identity_verifier.verify_identities', AsyncMock(return_value=([{'product_id': '42'}], [])))
    finish, cache = Mock(return_value=True), AsyncMock()
    monkeypatch.setattr(jobs, 'finish_job', finish)
    monkeypatch.setattr(jobs, 'redis_command', cache)
    assert await worker.analyze_job(job)
    kwargs = visual.call_args.kwargs
    assert kwargs['audio_transcript'] == 'relógio azul'
    assert kwargs['image_bytes'] == b'first-frame' and kwargs['extra_frame_bytes'] == [b'last-frame']
    assert kwargs['frame_timestamps_seconds'] == [1.0, 55.0]
    assert finish.call_args.kwargs['result']['approved_identities'] == [{'product_id': '42'}]
    if not original_stored:
        assert finish.call_args.kwargs['result']['media']['frame_storage_paths'] == ['private-frame', 'private-frame']
    cache.assert_awaited_once()
    download.return_value.close.assert_called_once()
    # Lost lease / operator correction: the worker must not publish into Redis.
    finish.return_value = False
    cache.reset_mock()
    assert not await worker.analyze_job(job)
    cache.assert_not_awaited()
