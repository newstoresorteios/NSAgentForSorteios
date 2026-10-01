"""Background Story evidence worker, drained by the durable queue dispatcher."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar

from app.config import get_settings
from app.stories import story_analysis_jobs as jobs
from app.stories.instagram_story_models import InstagramStoryContext, StoryVisualUnderstanding
from app.stories.instagram_story_media import (download_story_media_file,
    extract_video_frames_best_effort, SupabasePrivateStoryMediaStorage, StoryMediaError)
from app.ops.observability import log_event

_current_evidence = ContextVar('story_worker_evidence', default=None)


class StoryAnalysisPending(RuntimeError):
    def __init__(self, job_id):
        super().__init__('story_analysis_pending')
        self.job_id = job_id


def current_story_evidence():
    return _current_evidence.get()


def reset_story_evidence(token):
    _current_evidence.reset(token)


async def prepare_story_turn(incoming, workspace_id):
    """Run only inside the bound workspace, before conversation mutations/LLM calls."""
    from app.stories.instagram_story_intent import should_route_story_question
    from app.stories.instagram_story_service import story_rollout_allows
    from app.stories.story_tenant import resolve_story_tenant
    token = _current_evidence.set(None)
    try:
        from app.evaluation.context import current_evaluation
        if current_evaluation() is not None:
            return token
        story = incoming.instagram_story
        if not (getattr(get_settings(), 'instagram_story_worker_enabled', False) and get_settings().database_url and
                workspace_id and (incoming.raw or {}).get('inbound_id') and (incoming.raw or {}).get('inbox_id') and
                should_route_story_question(incoming) and story and story.story_media_id):
            return token
        tenant = await resolve_story_tenant(provider=story.provider,
            instagram_account_id=story.instagram_account_id, integration_id=None)
        if not tenant.ok or not tenant.tenant_id:
            return token
        allowed, _ = story_rollout_allows(tenant_id=tenant.tenant_id, story=story,
            conversation_id=incoming.conversation_id, incoming=incoming)
        if not allowed:
            return token
        # An operator-approved association is the stronger source of identity.
        from app.stories.story_product_repository import StoryProductRepository
        assoc = await asyncio.to_thread(StoryProductRepository().get_by_story,
            tenant_id=tenant.tenant_id, provider=story.provider,
            instagram_account_id=story.instagram_account_id, story_media_id=story.story_media_id)
        if assoc and assoc.match_status == 'manually_confirmed':
            return token
        job = await asyncio.to_thread(jobs.ensure_job, workspace_id=str(workspace_id),
            tenant_id=tenant.tenant_id, story=story, inbound_id=incoming.raw['inbound_id'])
        if job['status'] == 'failed':
            _current_evidence.set({'job': job, 'error': job.get('last_error') or 'analysis_failed'})
            return token
        result = await jobs.read_result(job)
        if result:
            _current_evidence.set({'job': job, 'result': result})
            return token
        raise StoryAnalysisPending(job['id'])
    except BaseException:
        _current_evidence.reset(token)
        raise


async def analyze_job(job):
    from app.persona.persona_runtime import load_persona_runtime, set_persona_runtime, reset_persona_runtime
    from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
    from app.ops.runtime_context import set_current_turn, reset_current_turn
    from app.ops.turn_runtime import TurnRuntimeContext, LLMCallBudget
    persona = await asyncio.to_thread(load_persona_runtime, workspace_id=str(job['workspace_id']))
    bundle = persona.configuration_bundle
    binding = bind_bundle(bundle, settings_from_bundle(get_settings(), bundle))
    persona_token = set_persona_runtime(persona)
    runtime = set_current_turn(TurnRuntimeContext(trace_id=f"story-job-{job['id']}",
        llm_budget=LLMCallBudget(max_calls=5, enforce=True)))
    media = None
    descriptor = {}
    try:
        source = await asyncio.to_thread(jobs.source_media, job)
        if not source:
            raise StoryMediaError('source_unavailable')
        metadata = source.get('channel_metadata') or {}
        source_story = source.get('source_story') or {}
        # A message can carry a photo attachment alongside its Story reply.
        # The Story's own media must take precedence over that attachment.
        url = source_story.get('url') or metadata.get('image_url') or ''
        attachment_is_story = not source_story.get('url') or source_story['url'] == metadata.get('image_url')
        private = job.get('media_storage_path') or (metadata.get('media_storage_path') if attachment_is_story else None)
        try:
            media = await download_story_media_file(url, private_path=private,
                workspace_id=str(job['workspace_id']))
        except StoryMediaError as exc:
            if exc.code not in {'http_403', 'http_404', 'http_410', 'url_missing', 'invalid_url'}:
                raise
            from app.stories.instagram_story_service import _hydrate_story_media
            story = await _hydrate_story_media(InstagramStoryContext(provider=job['provider'],
                instagram_account_id=job['instagram_account_id'], story_media_id=job['story_media_id']))
            refreshed = story.operational_media_url() or url
            if not refreshed:
                raise
            media = await download_story_media_file(refreshed)
            private = None
        path = private
        if not path:
            storage = SupabasePrivateStoryMediaStorage(bucket='conversation-media')
            path = await storage.put_private(content=media.path, content_type=media.content_type,
                sha256=media.sha256, tenant_id=str(job['workspace_id']))
            # Storage outages must not prevent analysis of the already downloaded video.
        descriptor = {'storage_path': path, 'mime': media.content_type,
                      'sha256': media.sha256, 'byte_count': media.byte_count}
        from app.ops.instagram_media_archive import _save
        await asyncio.to_thread(_save, source, path=path, mime=media.content_type, byte_count=media.byte_count)
        from app.stories.story_visual_analyzer import analyze_story_image
        from app.stories.story_video_audio import transcribe_story_video, StoryAudioEvidence
        audio = StoryAudioEvidence('not_applicable')
        frame_times = []
        if media.content_type.startswith('video/'):
            # Sequential decoding bounds native decoder memory use.
            frames = await asyncio.to_thread(extract_video_frames_best_effort, media.path,
                max_frames=get_settings().instagram_story_video_max_frames, frame_times=frame_times)
            if not frames:
                raise StoryMediaError('video_frames_unavailable')
            audio = await transcribe_story_video(media.path)
            if audio.status in {'unavailable', 'empty'}:
                raise StoryMediaError('audio_analysis_unavailable')
            if audio.status == 'duration_limit':
                raise StoryMediaError('duration_limit')
            media_type = 'video'
        else:
            from PIL import Image
            import io
            def image_frame():
                with Image.open(media.path) as image:
                    image = image.convert('RGB')
                    image.thumbnail((1600, 1600))
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=90)
                    return output.getvalue()
            frames = [await asyncio.to_thread(image_frame)]
            media_type = 'image'
        if not path:
            # Free Storage caps each object at 50 MB. Keep compact visual
            # evidence instead of rejecting a larger, already downloaded Story.
            import hashlib
            storage = SupabasePrivateStoryMediaStorage(bucket='conversation-media')
            frame_paths = []
            for frame in frames:
                frame_path = await storage.put_private(content=frame, content_type='image/jpeg',
                    sha256=hashlib.sha256(frame).hexdigest(), tenant_id=str(job['workspace_id']))
                if frame_path:
                    frame_paths.append(frame_path)
            descriptor['frame_storage_paths'] = frame_paths
        analysis = await analyze_story_image(image_bytes=frames[0], media_sha256=media.sha256,
            media_type=media_type, extra_frame_bytes=frames[1:],
            audio_transcript=audio.transcript, audio_status=audio.status, frame_timestamps_seconds=frame_times)
        from app.stories.story_product_matcher import match_story_to_catalog
        from app.tray.tray_tools import execute_tool
        candidates = await match_story_to_catalog(tenant_id=job['tenant_id'], analysis=analysis,
            execute_tool=execute_tool, media_bytes=None, store_url=None)
        if not candidates:
            # Do not distribute a week-long negative result caused by a transient
            # adapter outage or an index that has not finished synchronizing.
            raise StoryMediaError('catalog_candidates_unavailable')
        from app.stories.story_identity_verifier import verify_identities
        approved, reviews = await verify_identities(analysis=analysis, frames=frames,
            candidates=candidates, execute_tool=execute_tool)
        result = {'schema': jobs.EVIDENCE_VERSION, 'analysis': analysis.model_dump(mode='json'),
                  'approved_identities': approved, 'identity_reviews': reviews,
                  'candidates': [c.model_dump(mode='json') for c in candidates],
                  'media': descriptor}
        saved = await asyncio.to_thread(jobs.finish_job, job, result=result, media=descriptor)
        if saved:
            await jobs.redis_command('set', job, result)
        log_event('story.worker_completed', {'job_id': job['id'], 'frames': len(frames),
                  'audio_status': audio.status, 'approved_identities': len(approved), 'saved': saved})
        return saved
    except Exception as exc:
        code = exc.code if isinstance(exc, StoryMediaError) else type(exc).__name__
        await asyncio.to_thread(jobs.finish_job, job, error=code, media=descriptor)
        log_event('story.worker_failed', {'job_id': job['id'], 'code': code})
        return False
    finally:
        if media:
            media.close()
        reset_current_turn(runtime)
        reset_persona_runtime(persona_token)
        reset_bundle(binding)


async def process_story_analysis_batch(*, limit=1):
    if not get_settings().database_url:
        return {'claimed': 0, 'completed': 0}
    claimed = completed = 0
    for _ in range(max(1, min(limit, 2))):
        job = await asyncio.to_thread(jobs.claim_job)
        if not job:
            break
        claimed += 1
        try:
            completed += bool(await asyncio.wait_for(analyze_job(job), timeout=210))
        except TimeoutError:
            await asyncio.to_thread(jobs.finish_job, job, error='worker_timeout')
    return {'claimed': claimed, 'completed': completed}
