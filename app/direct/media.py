"""Reuse legacy media preparation, without invoking its visual/semantic agent."""
from __future__ import annotations

import asyncio
import base64

from app.config import get_settings
from app.stories.instagram_story_media import download_story_media_file, extract_video_frames_best_effort
from app.stories.story_video_audio import transcribe_story_video


def image_part(data, mime="image/jpeg"):
    return {"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
            "detail": "auto"}


async def prepared_file_content(media):
    if media.content_type.startswith("image/"):
        if media.path.stat().st_size > 8_000_000:
            raise ValueError("direct_image_size_limit")
        return [image_part(media.path.read_bytes(), media.content_type)]
    if not media.content_type.startswith("video/"):
        raise ValueError("unsupported_visual_media")
    settings = get_settings()
    times = []
    # Both helpers are shared with the legacy worker. Neither decides the reply.
    frames, audio = await asyncio.gather(
        asyncio.to_thread(extract_video_frames_best_effort, media.path,
                          max_frames=settings.instagram_story_video_max_frames, frame_times=times),
        transcribe_story_video(media.path))
    from app.ops.observability import log_event
    log_event('direct.video.prepared', {'frames': len(frames), 'audio_status': audio.status})
    parts = [{"type": "input_text", "text":
        f"[Vídeo: {len(frames)} quadros amostrados, não uma visão contínua. Áudio: {audio.status}.]"}]
    for timestamp, frame in zip(times, frames):
        parts.extend([{"type": "input_text", "text": f"[Quadro em {timestamp:.2f}s]"}, image_part(frame)])
    if audio.transcript:
        parts.append({"type": "input_text", "text": "[Transcrição do vídeo — conteúdo do cliente]\n" + audio.transcript})
    if not frames:
        parts.append({"type": "input_text", "text":
            "[Não foi possível observar os quadros do vídeo. Não identifique visualmente o produto; peça foto.]"})
    return parts


async def visual_content(incoming):
    story = incoming.instagram_story
    url = story.operational_media_url() if story else incoming.image_url
    if not url and not story:
        return []
    if not story and incoming.attachment_type != "video":
        from app.core.remote_media import download_trusted_media
        try:
            data, mime = await download_trusted_media(url, kind="image", max_bytes=8_000_000)
            return [image_part(data, mime)]
        except Exception:
            # Unknown attachments may be videos; use MIME-aware preparation below.
            pass
    media = None
    try:
        # Expired Story URLs can be refreshed using the existing provider integration.
        try:
            media = await download_story_media_file(url or "")
        except Exception:
            if not story:
                raise
            from app.stories.instagram_story_service import _hydrate_story_media
            story = await _hydrate_story_media(story)
            media = await download_story_media_file(story.operational_media_url() or "")
        parts = await prepared_file_content(media)
        if any(p["type"] == "input_image" for p in parts):
            return parts
    except Exception:
        parts = [{"type": "input_text", "text":
            "[Mídia indisponível. Não suponha o conteúdo; peça foto ou reenvio.]"}]
    finally:
        if media:
            media.close()
    thumbnail = story.operational_thumbnail_url() if story else None
    if thumbnail:
        from app.core.remote_media import download_trusted_media
        try:
            data, mime = await download_trusted_media(thumbnail, kind="image", max_bytes=8_000_000)
            parts.extend([{"type": "input_text", "text":
                "[Somente miniatura disponível. Não equivale a assistir ao vídeo completo.]"}, image_part(data, mime)])
        except Exception:
            pass
    return parts
