from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ops import instagram_media_archive as archive


@pytest.fixture
def row(monkeypatch):
    value = {'id': 1031, 'workspace_id': 'workspace-a', 'channel_metadata': {
        'image_url': 'https://lookaside.fbsbx.com/story?signature=secret'}}
    monkeypatch.setattr(archive, 'get_settings', lambda: SimpleNamespace(database_url='test', instagram_story_media_max_bytes=104857600))
    monkeypatch.setattr(archive, '_candidate', lambda _: value)
    monkeypatch.setattr(archive, '_save', Mock())
    monkeypatch.setattr(archive, 'log_event', Mock())
    return value


@pytest.mark.asyncio
async def test_archives_actual_video_bytes_under_resolved_workspace(monkeypatch, row):
    media = SimpleNamespace(path='private-temp-video', close=Mock(), sha256='a' * 64, content_type='video/mp4', byte_count=120)
    download = AsyncMock(return_value=media)
    monkeypatch.setattr(archive, 'download_story_media_file', download)
    storage = SimpleNamespace(put_private=AsyncMock(return_value='supabase://private/stored'), last_error=None)
    factory = Mock(return_value=storage)
    monkeypatch.setattr(archive, 'SupabasePrivateStoryMediaStorage', factory)
    assert await archive.archive_instagram_inbound_media(1031) == 'stored'
    assert download.call_args.kwargs['max_bytes'] == 104857600
    factory.assert_called_once_with(bucket='conversation-media')
    storage.put_private.assert_awaited_once_with(content='private-temp-video', content_type='video/mp4', sha256='a' * 64, tenant_id='workspace-a')
    media.close.assert_called_once()
    archive._save.assert_called_once_with(row, path='supabase://private/stored', mime='video/mp4', byte_count=120)


@pytest.mark.asyncio
async def test_already_stored_does_not_redownload(monkeypatch, row):
    row['channel_metadata']['media_storage_path'] = 'existing'
    download = AsyncMock()
    monkeypatch.setattr(archive, 'download_story_media_file', download)
    assert await archive.archive_instagram_inbound_media(1031) == 'already_stored'
    download.assert_not_called()


@pytest.mark.asyncio
async def test_expired_media_does_not_block_and_does_not_persist_secret(monkeypatch, row):
    monkeypatch.setattr(archive, 'download_story_media_file', AsyncMock(side_effect=RuntimeError('signature=secret')))
    assert await archive.archive_instagram_inbound_media(1031) == 'unavailable'
    assert archive._save.call_args.kwargs == {'error': 'RuntimeError'}
    assert 'secret' not in str(archive.log_event.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://evil.test/a.jpg', 'https://fbsbx.com.evil.test/a', 'http://fbcdn.net/a', 'https://user:pass@fbcdn.net/a'])
async def test_archive_rejects_non_meta_urls(monkeypatch, row, url):
    row['channel_metadata']['image_url'] = url
    download = AsyncMock()
    monkeypatch.setattr(archive, 'download_story_media_file', download)
    assert await archive.archive_instagram_inbound_media(1031) == 'unavailable'
    download.assert_not_called()


@pytest.mark.asyncio
async def test_unowned_inbound_does_not_download(monkeypatch, row):
    monkeypatch.setattr(archive, '_candidate', lambda _: None)
    download = AsyncMock()
    monkeypatch.setattr(archive, 'download_story_media_file', download)
    assert await archive.archive_instagram_inbound_media(1031) == 'unavailable'
    download.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_is_bounded_and_partial_failure_is_reported(monkeypatch):
    monkeypatch.setattr(archive, '_pending', lambda limit, workspace_id: [{'id': 1}, {'id': 2}])
    monkeypatch.setattr(archive, 'archive_instagram_inbound_media', AsyncMock(side_effect=['stored', 'unavailable']))
    result = await archive.backfill_instagram_media(limit=2, workspace_id='workspace-a')
    assert result == {'scanned': 2, 'stored': 1, 'already_stored': 0, 'unavailable': 1}


@pytest.mark.asyncio
async def test_storage_failure_keeps_sniffed_video_type_for_agent_and_central(monkeypatch, row):
    from app.models import IncomingMessage
    from app.stories.instagram_story_models import InstagramStoryContext
    incoming = IncomingMessage(attachment_type='image', instagram_story=InstagramStoryContext(media_type='image'))
    media = SimpleNamespace(path='private-temp-video', close=Mock(), sha256='a' * 64, content_type='video/mp4', byte_count=13_196_560)
    monkeypatch.setattr(archive, 'download_story_media_file', AsyncMock(return_value=media))
    storage = SimpleNamespace(put_private=AsyncMock(return_value=None), last_error='storage_upload_http_403')
    monkeypatch.setattr(archive, 'SupabasePrivateStoryMediaStorage', lambda **kw: storage)
    assert await archive.archive_instagram_inbound_media(1031, incoming=incoming) == 'unavailable'
    assert incoming.attachment_type == incoming.instagram_story.media_type == 'video'
    assert incoming.image_mime_type == 'video/mp4'
    archive._save.assert_called_once_with(row, error='storage_upload_http_403', mime='video/mp4', byte_count=13_196_560)


@pytest.mark.asyncio
async def test_retry_uses_archived_mime_without_downloading(monkeypatch, row):
    from app.models import IncomingMessage
    row['channel_metadata'].update(media_storage_path='existing', media_content_type='video/mp4')
    incoming = IncomingMessage(attachment_type='image')
    assert await archive.archive_instagram_inbound_media(1031, incoming=incoming) == 'already_stored'
    assert incoming.attachment_type == 'video'
