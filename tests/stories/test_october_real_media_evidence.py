"""Private original media replay; supply STORY_REAL_MEDIA_DIR for local execution.

Vision outputs are session annotations. Catalogue lookup is explicitly unavailable
instead of fabricating candidate identities. No model or production calls occur.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings
from app.stories.instagram_story_models import StoryVisualUnderstanding
from app.stories.story_video_audio import StoryAudioEvidence
from app.stories import story_analysis_worker as worker, story_analysis_jobs as jobs

ANNOTATIONS = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/story_media/october_annotations.json').read_text(encoding='utf-8'))


def test_incident_media_annotations_keep_targets_and_evidence_limits():
    cases = {r['case_id']: r for r in ANNOTATIONS['cases']}
    assert set(cases) == {'1127', '1130', '1143', '1166', '1169', '1179'}
    for case in cases.values():
        StoryVisualUnderstanding.model_validate(case['analysis'])
        assert len(case['source_sha256']) == 64
        assert case['approved_exact_identities'] == []
        assert case['audio_reviewed'] is False
    assert cases['1127']['analysis']['watch_count'] == 2
    assert cases['1143']['analysis']['watch_count'] == 4
    assert cases['1169']['source_sha256'] == cases['1143']['source_sha256']
    assert cases['1179']['input_envelope']['replied_to_story'] is False
    assert ANNOTATIONS['text_followups'][0]['has_new_media'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ANNOTATIONS['cases'], ids=lambda c: c['case_id'])
async def test_original_media_ingestion_with_annotated_vision_and_unavailable_catalog(monkeypatch, case):
    directory = os.getenv('STORY_REAL_MEDIA_DIR')
    if not directory:
        pytest.skip('Private original files not distributed; set STORY_REAL_MEDIA_DIR to audited local archive')
    source = Path(directory) / case['private_fixture_filename']
    assert source.exists(), 'Missing private fixture; do not replace it with synthetic bytes'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == case['source_sha256']
    assert source.stat().st_size == case['source_bytes']
    from app.stories import story_visual_analyzer as vision
    settings = Settings(_env_file=None)
    settings.instagram_story_video_frame_analysis_enabled = True
    settings.instagram_story_video_max_frames = 7
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    monkeypatch.setattr(vision, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.stories.instagram_story_media.get_settings', lambda: settings)
    monkeypatch.setattr('app.persona.persona_runtime.load_persona_runtime',
                        lambda **kw: SimpleNamespace(configuration_bundle={}))
    monkeypatch.setattr(jobs, 'source_media', lambda job: {'id': 1, 'channel_metadata': {}})
    media = SimpleNamespace(path=source, content_type=case['source_mime'],sha256=case['source_sha256'],
                            byte_count=case['source_bytes'], close=Mock())
    monkeypatch.setattr(worker, 'download_story_media_file', AsyncMock(return_value=media))
    monkeypatch.setattr('app.ops.instagram_media_archive._save', Mock())
    monkeypatch.setattr('app.stories.story_video_audio.transcribe_story_video',
                        AsyncMock(return_value=StoryAudioEvidence('unavailable')))
    captured=[]
    async def annotated_vision(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(parsed=StoryVisualUnderstanding.model_validate(case['analysis']),metrics=None)
    monkeypatch.setattr(vision,'parse_structured_output',annotated_vision)
    lookup=AsyncMock(return_value=([],{}))
    monkeypatch.setattr('app.stories.story_catalog_evidence.match_scene_catalog',lookup)
    finish=Mock(return_value=True)
    monkeypatch.setattr(jobs,'finish_job',finish)
    job={'id':1,'workspace_id':'offline-w','tenant_id':'offline-t','provider':'meta',
         'instagram_account_id':'offline-a','story_media_id':'offline-s',
         'media_storage_path':'offline-private-original','generation':'offline-g','lease_owner':'offline-o','attempts':1}
    assert await worker.analyze_job(job) is False
    assert finish.call_args.kwargs['error'] == 'catalog_candidates_unavailable'
    parts=[p for p in captured[0]['messages'][1]['content'] if p.get('type')=='image_url']
    assert len(parts) == (7 if case['source_mime'].startswith('video') else 1)
    if case['reviewed_frame_sha256']:
        assert [hashlib.sha256(base64.b64decode(p['image_url']['url'].split(',',1)[1])).hexdigest()
                for p in parts] == case['reviewed_frame_sha256']
    analyzed=lookup.call_args.kwargs['analysis']
    assert analyzed.watch_count == case['analysis']['watch_count']
    assert analyzed.frames_analyzed == len(parts)
    assert analyzed.visible_references == []
    assert 'result' not in finish.call_args.kwargs
