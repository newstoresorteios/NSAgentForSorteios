"""Exercise the published settings path that the 13 MB production incident missed."""
import json
from pathlib import Path

import httpx
import pytest

from app.configuration.runtime import bind_bundle, reset_bundle, settings_from_bundle
from app.core.config import Settings, get_settings
from app.stories.instagram_story_media import StoryMediaError, download_story_media


ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / 'sql/seeds/operator_catalog.json').read_text(encoding='utf-8'))
STORY_FIELDS = [field for field in CATALOG if field['key'].startswith('instagram_story_')]


def test_story_catalog_tracks_runtime_defaults_and_bounds():
    """A release changing Story defaults must update the persisted catalog too."""
    fields = {field['key']: field for field in STORY_FIELDS}
    schema = Settings.model_json_schema()['properties']
    for name, model_field in Settings.model_fields.items():
        if not name.startswith('instagram_story_'):
            continue
        assert name in fields, f'Missing published control: {name}'
        prop = schema[model_field.alias]
        definition = fields[name]
        assert definition['target'] == 'setting'
        assert definition['attribute'] == name
        assert definition['default'] == prop['default'], name
        assert definition.get('min') == prop.get('minimum'), name
        assert definition.get('max') == prop.get('maximum'), name


def test_migration_publishes_same_controls_as_the_catalog():
    migration = (ROOT / 'supabase/migrations/20261001000241_sync_story_multimodal_configuration.sql').read_text(encoding='utf-8')
    definitions = json.loads(migration.split('$story_catalog$')[1])
    published = json.loads(migration.split('$story_values$')[1])
    catalog = {field['key']: field for field in CATALOG}
    for definition in definitions:
        assert definition == catalog[definition['key']]
        assert published[definition['key']] == definition['default']


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('with_content_length', [True, False])
async def test_real_stream_limit_uses_published_workspace_values(monkeypatch, legacy, with_content_length):
    """Don't mock _stream_once: exercise Content-Length AND streamed byte checks."""
    from app.stories import instagram_story_media as media

    values = {field['key']: field['default'] for field in STORY_FIELDS}
    if legacy:
        values.update(instagram_story_media_max_bytes=12_582_912,
                      instagram_story_video_frame_analysis_enabled=False,
                      instagram_story_video_max_frames=3)
    bundle = {'fields': STORY_FIELDS, 'values': values}
    effective = settings_from_bundle(Settings(_env_file=None, OPENAI_API_KEY='', DATABASE_URL=''), bundle)
    token = bind_bundle(bundle, effective)
    content = b'\x00\x00\x00\x18ftypmp42' + bytes(13_196_560 - 12)
    real_client = httpx.AsyncClient
    def respond(request):
        response = httpx.Response(200, headers={'content-type': 'video/mp4'}, content=content)
        if not with_content_length:
            del response.headers['content-length']
        return response
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(media, 'validate_story_media_url', lambda url: (url, ['157.240.0.1']))
    try:
        assert get_settings().instagram_story_video_frame_analysis_enabled is (not legacy)
        if legacy:
            with pytest.raises(StoryMediaError, match='file_too_large'):
                await download_story_media('https://scontent.cdninstagram.com/story.mp4', persist=False)
        else:
            result = await download_story_media('https://scontent.cdninstagram.com/story.mp4', persist=False)
            assert result.byte_count == 13_196_560
            assert result.content_type == 'video/mp4'
            assert get_settings().instagram_story_video_max_frames == 8
            assert get_settings().instagram_story_video_audio_enabled is True
    finally:
        reset_bundle(token)


def test_operator_can_still_deliberately_disable_video_analysis():
    bundle = {'fields': STORY_FIELDS,
              'values': {'instagram_story_video_frame_analysis_enabled': False}}
    settings = settings_from_bundle(Settings(_env_file=None), bundle)
    assert settings.instagram_story_video_frame_analysis_enabled is False
