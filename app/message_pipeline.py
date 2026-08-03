from __future__ import annotations

from app.config import get_settings
from app.agent_contracts import build_agent_decision, evaluate_policy
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_identity import (
    resolve_person_key_candidates,
    upsert_customer_identity_links,
)
from app.db import load_commerce_conversation_state, persist_customer_commerce_session
from app.observability import (
    log_event,
    redact_text,
    summarize_commerce_state,
)
from app.working_memory import build_working_memory
from app.factual_validator import apply_factual_validation
from app.handoff_service import enrich_handoff_metadata
from app.models import AgentResult, IncomingMessage
from app.openai_agent import generate_agent_reply_async
from app.quality_judge import attach_judge_report, run_quality_judge
from app.response_composer import compose_outbound_reply
from app.runtime_context import get_current_turn, runtime_stage
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
            "has_audio_url": bool(incoming.audio_url),
        })
        incoming.transcription_failed = True
        incoming.text = ""
        incoming.input_modality = "audio"
        return incoming

    print("[audio.inbound] transcribed", {
        "chars": len(transcribed),
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
        })
        return result

    result.reply_modality = "audio"
    result.reply_audio_bytes = audio_bytes
    result.reply_audio_mime_type = mime_type
    result.reply_audio_url = audio_url
    return result


async def process_incoming_message(incoming: IncomingMessage, customer_context: dict) -> AgentResult:
    settings = get_settings()
    runtime = get_current_turn()
    if runtime is not None:
        runtime.channel = incoming.channel or runtime.channel
        runtime.conversation_key = (
            incoming.conversation_id
            or incoming.sender_key
            or incoming.visitor_id
            or incoming.sender_phone
            or runtime.conversation_key
        )
        try:
            runtime.inbound_id = int((incoming.raw or {}).get("inbound_id"))
        except (TypeError, ValueError):
            pass
    customer_context = enrich_customer_context(customer_context)
    with runtime_stage("prepare_incoming"):
        incoming = await prepare_incoming_message(incoming)
    raw_inbound_id = (incoming.raw or {}).get("inbound_id")
    try:
        inbound_id = int(raw_inbound_id) if raw_inbound_id is not None else None
    except (TypeError, ValueError):
        inbound_id = None
    inbound_snapshot = {
        "channel": incoming.channel,
        "conversation_id_present": bool(incoming.conversation_id),
        "sender_key_present": bool(incoming.sender_key),
        "sender_phone_present": bool(incoming.sender_phone),
        "sender_name_present": bool(incoming.sender_name),
        "input_modality": incoming.input_modality,
        "text_chars": len(incoming.text or ""),
        "text_preview": redact_text(incoming.text, max_chars=220),
        "customer_found": bool(customer_context.get("found")),
    }
    if runtime is not None:
        runtime.inbound_snapshot = inbound_snapshot
    log_event("turn.start", inbound_snapshot)
    state_lookup = {
        "conversation_id": incoming.conversation_id,
        "sender_phone": incoming.sender_phone,
        "before_inbound_id": inbound_id,
        "sender_key": incoming.sender_key,
    }
    with runtime_stage("load_context"):
        commerce_state = CommerceConversationState.from_payload(
            load_commerce_conversation_state(**state_lookup)
        )
    working_memory = build_working_memory(commerce_state)
    customer_context = {
        **customer_context,
        "_commerce_state": commerce_state.model_dump(mode="json"),
        "_working_memory": working_memory,
    }
    context_snapshot = {
        **summarize_commerce_state(commerce_state),
        "working_memory_payment_pending": bool(working_memory.get("payment_pending")),
        "working_memory_has_open_order": bool(working_memory.get("has_open_order")),
        "person_key_aliases": len(
            resolve_person_key_candidates(
                sender_key=incoming.sender_key,
                sender_phone=incoming.sender_phone,
                state=commerce_state,
            )
        ),
    }
    if runtime is not None:
        runtime.context_snapshot = context_snapshot
    log_event("context.loaded", context_snapshot)
    print("[sales.context.state]", {
        "active_domain": commerce_state.active_domain,
        "has_active_product": commerce_state.active_product is not None,
        "presented_product_count": len(commerce_state.last_presented_products),
        "active_topic_present": bool(commerce_state.active_topic),
        "purchase_stage": commerce_state.purchase_stage,
        "has_cart_session": bool(commerce_state.cart_session_id),
        "pending_action": commerce_state.pending_action,
        "pending_action_has_product": bool(commerce_state.pending_action_product_ids),
        "has_open_order": bool(working_memory.get("has_open_order")),
        "payment_pending": bool(working_memory.get("payment_pending")),
    })

    user_id = customer_context.get("user_id")
    if customer_context.get("found") and user_id:
        learn_from_incoming_message(
            int(user_id),
            incoming.text,
            customer_context.get("name"),
        )
        customer_context = enrich_customer_context(customer_context)

    with runtime_stage("agent_decision"):
        result = await generate_agent_reply_async(incoming, customer_context)
    commerce_state = evolve_commerce_state(commerce_state, result)
    result.response_metadata["commerce_state"] = commerce_state.model_dump(mode="json")
    result.response_metadata["working_memory"] = build_working_memory(commerce_state)
    upsert_customer_identity_links(incoming, commerce_state)
    persist_customer_commerce_session(
        person_keys=resolve_person_key_candidates(
            sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone,
            state=commerce_state,
        ),
        commerce_state=commerce_state.model_dump(mode="json"),
        channel=incoming.channel,
        conversation_id=incoming.conversation_id,
        sender_key=incoming.sender_key,
        sender_phone=incoming.sender_phone,
    )
    decision = build_agent_decision(
        incoming,
        result,
        openai_call_count=runtime.openai_call_count if runtime else 0,
    )
    policy_snapshot = evaluate_policy(
        decision,
        mode=getattr(settings, "agent_policy_mode", "shadow"),
    )
    result.response_metadata["decision_snapshot"] = (
        policy_snapshot.model_dump(mode="json")
    )
    trusted_fact_domains = {
        domain.strip().lower()
        for domain in getattr(
            settings,
            "agent_trusted_fact_domains",
            "sorteionewstore.com.br,newstoresorteios.com.br",
        ).split(",")
        if domain.strip()
    }
    result = apply_factual_validation(
        result,
        decision=decision,
        mode=getattr(
            settings,
            "agent_factual_validation_mode",
            "shadow",
        ),
        trusted_domains=trusted_fact_domains,
    )
    result = enrich_handoff_metadata(incoming, result)
    validation = result.response_metadata.get("factual_validation") or {}
    with runtime_stage("quality_judge"):
        judge_report = await run_quality_judge(
            incoming,
            result,
            mode=getattr(settings, "agent_quality_judge_mode", "off"),
            risk_score=decision.risk.score,
            factual_valid=bool(validation.get("valid", True)),
            openai_call_count=runtime.openai_call_count if runtime else 0,
        )
    result = attach_judge_report(result, judge_report)
    max_reply_chars = getattr(settings, "max_reply_chars", 900)
    result = compose_outbound_reply(
        incoming,
        result,
        max_reply_chars=max_reply_chars,
    )
    if runtime is not None:
        runtime.execution_path = decision.execution_path
        runtime.risk_score = decision.risk.score
        if validation.get("fallback_applied"):
            runtime.register_fallback("factual_validation_failed")
        if judge_report.applied:
            runtime.register_fallback("quality_judge_failed")

    response_metadata = result.response_metadata or {}
    outbound_snapshot = {
        "domain": response_metadata.get("domain"),
        "goal": response_metadata.get("goal"),
        "intent": result.intent,
        "response_source": response_metadata.get("response_source"),
        "used_openai_interpreter": bool(response_metadata.get("used_openai_interpreter")),
        "used_openai_responder": bool(response_metadata.get("used_openai_responder")),
        "used_tray": bool(response_metadata.get("used_tray")),
        "fallback_reason": response_metadata.get("fallback_reason"),
        "safety_reason": result.safety_reason,
        "handoff_required": result.handoff_required,
        "reply_chars": len(result.reply_text or ""),
        "reply_preview": redact_text(result.reply_text, max_chars=280),
        "final_order_id": (response_metadata.get("commerce_state") or {}).get("order_id"),
        "final_pending_action": (response_metadata.get("commerce_state") or {}).get(
            "pending_action"
        ),
        "tray_tools": [
            item.get("tool")
            for item in (runtime.tray_calls if runtime else [])
        ],
        "openai_call_types": [
            item.get("call_type")
            for item in (runtime.openai_calls if runtime else [])
        ],
    }
    if runtime is not None:
        runtime.outbound_snapshot = outbound_snapshot
    print("[agent.response]", {
        "domain": outbound_snapshot["domain"],
        "goal": outbound_snapshot["goal"],
        "response_source": outbound_snapshot["response_source"],
        "used_openai_interpreter": outbound_snapshot["used_openai_interpreter"],
        "used_openai_responder": outbound_snapshot["used_openai_responder"],
        "used_tray": outbound_snapshot["used_tray"],
        "fallback_reason": outbound_snapshot["fallback_reason"],
        "safety_reason": outbound_snapshot["safety_reason"],
        "handoff_required": outbound_snapshot["handoff_required"],
        "judge_triggered": bool(
            (response_metadata.get("quality_judge") or {}).get("triggered")
        ),
    })
    log_event("turn.end", outbound_snapshot)

    if customer_context.get("found") and user_id:
        record_interaction_memory(int(user_id), result.intent, incoming.text)

    with runtime_stage("enrich_result"):
        enriched = await enrich_agent_result(incoming, result)
        return compose_outbound_reply(
            incoming,
            enriched,
            max_reply_chars=max_reply_chars,
        )
