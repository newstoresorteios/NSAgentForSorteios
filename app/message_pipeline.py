from __future__ import annotations

from app.config import get_settings
from app.models import AgentResult, IncomingMessage
from app.openai_agent import generate_agent_reply
from app.audio_service import should_transcribe_incoming


async def prepare_incoming_message(incoming: IncomingMessage) -> IncomingMessage:
    settings = get_settings()
    if not settings.audio_inbound_enabled:
        return incoming
    if not should_transcribe_incoming(incoming.text, incoming.audio_url, incoming.audio_filename):
        return incoming

    from app.audio_service import transcribe_audio_url

    try:
        transcribed = await transcribe_audio_url(
            incoming.audio_url or "",
            filename=incoming.audio_filename,
        )
    except Exception as exc:
        print("[audio.inbound] transcription_failed", {
            "error_type": type(exc).__name__,
            "message": str(exc)[:180],
            "has_audio_url": bool(incoming.audio_url),
        })
        incoming.text = (
            "Recebi seu áudio, mas não consegui transcrever agora. "
            "Pode repetir por texto ou enviar outro áudio?"
        )
        incoming.input_modality = "audio"
        return incoming

    incoming.text = transcribed
    incoming.input_modality = "audio"
    return incoming


async def enrich_agent_result(incoming: IncomingMessage, result: AgentResult) -> AgentResult:
    settings = get_settings()
    if incoming.input_modality != "audio":
        return result
    if not settings.audio_outbound_enabled:
        return result

    from app.audio_service import synthesize_reply_audio
    from app.supabase_storage import upload_public_audio

    try:
        audio_bytes = synthesize_reply_audio(result.reply_text)
        audio_url = await upload_public_audio(audio_bytes, content_type="audio/mpeg")
    except Exception as exc:
        print("[audio.outbound] tts_or_upload_failed", {
            "error_type": type(exc).__name__,
            "message": str(exc)[:180],
        })
        return result

    result.reply_modality = "audio"
    result.reply_audio_bytes = audio_bytes
    result.reply_audio_mime_type = "audio/mpeg"
    result.reply_audio_url = audio_url
    return result


async def process_incoming_message(incoming: IncomingMessage, customer_context: dict) -> AgentResult:
    incoming = await prepare_incoming_message(incoming)
    result = generate_agent_reply(incoming, customer_context)
    return await enrich_agent_result(incoming, result)
