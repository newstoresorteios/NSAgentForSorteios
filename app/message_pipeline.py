from __future__ import annotations

from app.config import get_settings
from app.models import AgentResult, IncomingMessage
from app.openai_agent import generate_agent_reply_async
from app.user_preferences import enrich_customer_context, learn_from_incoming_message, record_interaction_memory
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
            "audio_filename": incoming.audio_filename,
        })
        incoming.transcription_failed = True
        incoming.text = ""
        incoming.input_modality = "audio"
        return incoming

    print("[audio.inbound] transcribed", {
        "chars": len(transcribed),
        "preview": transcribed[:120],
    })
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
        audio_bytes, mime_type, filename = synthesize_reply_audio(result.reply_text)
        audio_url = await upload_public_audio(audio_bytes, content_type=mime_type, filename=filename)
    except Exception as exc:
        print("[audio.outbound] tts_or_upload_failed", {
            "error_type": type(exc).__name__,
            "message": str(exc)[:180],
        })
        return result

    result.reply_modality = "audio"
    result.reply_audio_bytes = audio_bytes
    result.reply_audio_mime_type = mime_type
    result.reply_audio_url = audio_url
    return result


async def process_incoming_message(incoming: IncomingMessage, customer_context: dict) -> AgentResult:
    customer_context = enrich_customer_context(customer_context)
    incoming = await prepare_incoming_message(incoming)

    user_id = customer_context.get("user_id")
    if customer_context.get("found") and user_id:
        learn_from_incoming_message(
            int(user_id),
            incoming.text,
            customer_context.get("name"),
        )
        customer_context = enrich_customer_context(customer_context)

    result = await generate_agent_reply_async(incoming, customer_context)

    response_metadata = result.response_metadata or {}
    print("[agent.response]", {
        "domain": response_metadata.get("domain"),
        "goal": response_metadata.get("goal"),
        "response_source": response_metadata.get("response_source"),
        "used_openai_interpreter": bool(response_metadata.get("used_openai_interpreter")),
        "used_openai_responder": bool(response_metadata.get("used_openai_responder")),
        "used_tray": bool(response_metadata.get("used_tray")),
        "fallback_reason": response_metadata.get("fallback_reason"),
        "safety_reason": result.safety_reason,
    })

    if customer_context.get("found") and user_id:
        record_interaction_memory(int(user_id), result.intent, incoming.text)

    return await enrich_agent_result(incoming, result)
