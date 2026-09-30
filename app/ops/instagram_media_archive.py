"""Archive received Meta media without re-running the agent or sending replies."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from app.config import get_settings
from app.db import get_conn
from app.ops.observability import log_event
from app.stories.instagram_story_media import download_story_media, SupabasePrivateStoryMediaStorage, StoryMediaError


def _classify_incoming(incoming, mime):
    if incoming is None or not str(mime or '').startswith(('image/', 'video/')):
        return
    incoming.attachment_type = mime.split('/', 1)[0]
    incoming.image_mime_type = mime
    if incoming.instagram_story is not None:
        incoming.instagram_story.media_type = incoming.attachment_type


def _candidate(inbound_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, workspace_id, channel_metadata
                FROM public.ai_inbound_messages
                WHERE id = %s AND provider = 'meta' AND channel = 'instagram'
                  AND workspace_id IS NOT NULL
            """, (inbound_id,))
            return cur.fetchone()


def _save(row, *, path=None, mime=None, byte_count=None, error=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.ai_inbound_messages
                SET channel_metadata = COALESCE(channel_metadata, '{}'::jsonb)
                    || jsonb_build_object('media_archive_checked_at', NOW(),
                                          'media_archive_error', %(error)s::text,
                                          'media_archive_attempts', COALESCE((channel_metadata->>'media_archive_attempts')::integer, 0) + 1)
                    || CASE WHEN %(mime)s::text IS NOT NULL THEN jsonb_build_object(
                        'media_content_type', %(mime)s::text,
                        'attachment_type', split_part(%(mime)s::text, '/', 1),
                        'media_byte_count', %(byte_count)s::integer) ELSE '{}'::jsonb END
                    || CASE WHEN %(path)s::text IS NOT NULL THEN jsonb_build_object(
                        'media_storage_path', %(path)s::text,
                        'media_content_type', %(mime)s::text,
                        'media_byte_count', %(byte_count)s::integer) ELSE '{}'::jsonb END
                WHERE id = %(id)s AND workspace_id = %(workspace_id)s::uuid
                  AND NULLIF(channel_metadata->>'media_storage_path', '') IS NULL
            """, {"id": row['id'], "workspace_id": str(row['workspace_id']),
                  "path": path, "mime": mime, "byte_count": byte_count, "error": error})
        conn.commit()


async def archive_instagram_inbound_media(inbound_id: int, *, incoming=None) -> str:
    """Persist a workspace-owned private copy; network failure must not block chat."""
    if not getattr(get_settings(), 'database_url', None):
        return 'unavailable'
    row = await asyncio.to_thread(_candidate, inbound_id)
    if not row:
        return 'unavailable'
    metadata = row.get('channel_metadata') or {}
    _classify_incoming(incoming, metadata.get('media_content_type'))
    if metadata.get('media_storage_path'):
        return 'already_stored'
    url = metadata.get('image_url') or ''
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        await asyncio.to_thread(_save, row, error='invalid_meta_media_url')
        return 'unavailable'
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
        host == suffix or host.endswith('.' + suffix)
        for suffix in ('fbcdn.net', 'cdninstagram.com', 'fbsbx.com')
    ):
        await asyncio.to_thread(_save, row, error='invalid_meta_media_url')
        return 'unavailable'
    media = None
    try:
        # This existing bucket is private and separate from public audio storage.
        media = await asyncio.wait_for(download_story_media(
            url, tenant_id=str(row['workspace_id']), persist=False, max_bytes=16_777_216,
        ), timeout=15)
        # Classify even if Storage fails: an opaque MP4 is never a photo.
        _classify_incoming(incoming, media.content_type)
        storage = SupabasePrivateStoryMediaStorage(bucket='conversation-media')
        path = await asyncio.wait_for(storage.put_private(
            content=media.content, content_type=media.content_type,
            sha256=media.sha256, tenant_id=str(row['workspace_id']),
        ), timeout=15)
        if not path:
            raise StoryMediaError(storage.last_error or 'private_storage_unavailable')
        await asyncio.to_thread(_save, row, path=path,
                                mime=media.content_type, byte_count=media.byte_count)
        return 'stored'
    except Exception as exc:
        # Never persist or log URLs, access tokens or raw HTTP responses.
        code = exc.code if isinstance(exc, StoryMediaError) else type(exc).__name__
        fields = {'mime': media.content_type, 'byte_count': media.byte_count} if media else {}
        await asyncio.to_thread(_save, row, error=code, **fields)
        log_event('instagram.media_archive.unavailable', {'error_type': code})
        return 'unavailable'


def _pending(limit, workspace_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM public.ai_inbound_messages
                WHERE provider = 'meta' AND channel = 'instagram'
                  AND workspace_id IS NOT NULL
                  AND (%(workspace_id)s::uuid IS NULL OR workspace_id = %(workspace_id)s::uuid)
                  AND created_at >= NOW() - INTERVAL '30 days'
                  AND NULLIF(channel_metadata->>'image_url', '') IS NOT NULL
                  AND NULLIF(channel_metadata->>'media_storage_path', '') IS NULL
                  AND COALESCE((channel_metadata->>'media_archive_attempts')::integer, 0) < 3
                  AND (NULLIF(channel_metadata->>'media_archive_checked_at', '') IS NULL
                       OR (channel_metadata->>'media_archive_checked_at')::timestamptz < NOW() - INTERVAL '5 minutes')
                ORDER BY channel_metadata->>'media_archive_checked_at' NULLS FIRST, created_at DESC
                LIMIT %(limit)s
            """, {'limit': max(1, min(limit, 10)), 'workspace_id': workspace_id})
            return list(cur.fetchall() or [])


async def backfill_instagram_media(*, limit=6, workspace_id=None):
    candidates = await asyncio.to_thread(_pending, limit, workspace_id)
    semaphore = asyncio.Semaphore(3)
    async def one(row):
        async with semaphore:
            try:
                return await archive_instagram_inbound_media(int(row['id']))
            except Exception as exc:
                log_event('instagram.media_archive.failed', {'error_type': type(exc).__name__})
                return 'unavailable'
    statuses = await asyncio.gather(*(one(row) for row in candidates))
    return {'scanned': len(candidates), 'stored': statuses.count('stored'),
            'already_stored': statuses.count('already_stored'), 'unavailable': statuses.count('unavailable')}
