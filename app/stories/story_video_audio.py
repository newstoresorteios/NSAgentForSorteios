"""Bounded, private audio evidence. Never authority for prices or stock."""
from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import dataclass

from app.config import get_settings
from app.ops.observability import log_event


@dataclass
class StoryAudioEvidence:
    status: str
    transcript: str = ""


def extract_audio(content: bytes, max_seconds: int) -> tuple[bytes, str]:
    """Use the installed decoder; keep PCM/WAV in memory, with no public uploads."""
    from decord import AudioReader, VideoReader, cpu
    import numpy as np

    video = VideoReader(io.BytesIO(content), ctx=cpu(0), num_threads=1)
    fps = float(video.get_avg_fps())
    if fps <= 0 or len(video) / fps > max_seconds:
        return b"", "duration_limit"
    reader = AudioReader(io.BytesIO(content), ctx=cpu(0), sample_rate=16000, mono=True)
    if reader.shape[1] > max_seconds * 16000:
        return b"", "duration_limit"
    samples = reader[:].asnumpy().reshape(-1)
    if samples.size < 1600 or not np.isfinite(samples).all() or float(np.sqrt(np.mean(samples ** 2))) < 0.003:
        return b"", "silent"
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return buffer.getvalue(), "ready"


async def transcribe_story_video(content: bytes) -> StoryAudioEvidence:
    settings = get_settings()
    if not getattr(settings, "instagram_story_video_audio_enabled", False):
        return StoryAudioEvidence("disabled")
    if not settings.openai_api_key:
        return StoryAudioEvidence("unavailable")
    try:
        if not content or len(content) > settings.instagram_story_media_max_bytes:
            return StoryAudioEvidence("size_limit")
        audio, status = await asyncio.wait_for(asyncio.to_thread(
            extract_audio, content, getattr(settings, "instagram_story_video_max_seconds", 120)), timeout=8)
        if not audio:
            return StoryAudioEvidence(status)
        from openai import AsyncOpenAI
        from app.llm.openai_runtime import execute_openai_call
        timeout = getattr(settings, "instagram_story_audio_timeout_seconds", 12)
        async with AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=timeout) as client:
            result = await execute_openai_call(
                call_type="story_audio_transcription", model=settings.openai_transcribe_model,
                timeout_seconds=timeout,
                operation=lambda: client.audio.transcriptions.create(
                    model=settings.openai_transcribe_model,
                    file=("story.wav", audio, "audio/wav"),
                ),
            )
        text = str(getattr(result, "text", "") or "").strip()[:6000]
        return StoryAudioEvidence("transcribed" if text else "empty", text)
    except Exception as exc:
        # No tokens, signed URLs, raw audio, or transcript in logs.
        log_event("story.audio_unavailable", {"error_type": type(exc).__name__})
        return StoryAudioEvidence("unavailable")
