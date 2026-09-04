"""Sync NewStoreAgent path — not on the live WhatsApp webhook.

Do not grow this module. Audio-fail on the live door already stops here.
Names are resolved on ``app.agents.door`` so monkeypatches keep working.
"""

from __future__ import annotations

from openai import APIError

from app.models import AgentResult, IncomingMessage
from app.ops.turn_runtime import LLMCallBudgetExceeded


def _door():
    import app.agents.door as door_mod

    return door_mod


def generate_openai_reply(
    message: IncomingMessage,
    customer_context: dict,
    facts: dict,
) -> AgentResult:
    door = _door()
    settings = door.get_settings()
    if not settings.openai_api_key:
        return AgentResult(
            reply_text=door._non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason="openai_api_key_missing",
        )

    from app.llm.prompt_compiler import legacy_contract_extra_blocks, resolve_system_instructions

    user_input = door.build_agent_input(message, customer_context, facts)
    system_instructions = resolve_system_instructions(
        fallback_instructions=door.SYSTEM_INSTRUCTIONS,
        incoming=message,
        extra_system_blocks=legacy_contract_extra_blocks(
            door.SYSTEM_INSTRUCTIONS,
            tag="legacy_agent_contract",
        ),
    )
    legacy_messages = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": user_input},
    ]
    try:
        from app.llm.openai_errors import OpenAIGatewayError
        from app.llm.openai_gateway import generate_text_sync

        text_result = generate_text_sync(
            model=settings.openai_model,
            messages=legacy_messages,
            temperature=0.3,
            call_type="legacy",
        )
        content = text_result.text
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded) as exc:
        status_code = getattr(exc, "status_code", None)
        print("[openai.agent] request_failed", {
            "status_code": status_code,
            "error_type": type(exc).__name__,
            "model": settings.openai_model,
            "message": door._sanitize_log_message(str(exc)),
        })
        return AgentResult(
            reply_text=door._non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason=f"openai_error_{status_code or type(exc).__name__}",
        )

    reply = door._truncate(
        content or door._non_handoff_fallback(message, facts),
        settings.max_reply_chars,
    )
    return AgentResult(
        reply_text=reply,
        intent=str(facts.get("primary_intent") or "general_support"),
        handoff_required=False,
    )


def generate_agent_reply(message: IncomingMessage, customer_context: dict) -> AgentResult:
    door = _door()
    from app.channels.audio_service import (
        audio_transcription_failed_result,
        inbound_audio_failed,
    )

    if inbound_audio_failed(message):
        return audio_transcription_failed_result()

    blocked_reason = door.detect_blocked_request(message.text)
    if blocked_reason:
        return AgentResult(
            reply_text=door.default_safe_handoff(),
            intent="handoff",
            handoff_required=True,
            safety_reason=blocked_reason,
        )

    scope = door.deterministic_scope(message.text)
    print("[agent.scope]", {"domain": scope.get("domain")})
    if scope.get("domain") == "out_of_scope":
        return AgentResult(
            reply_text=door.OUT_OF_SCOPE_REPLY,
            intent="out_of_scope",
            handoff_required=False,
            safety_reason="scope_refusal",
        )
    if scope.get("domain") == "greeting":
        return AgentResult(
            reply_text=door.choose_greeting_reply(None),
            intent="general",
            handoff_required=False,
        )
    primary_intent = door.detect_primary_intent(message.text)
    print("[agent.route]", {
        "inbound_id": (message.raw or {}).get("inbound_id"),
        "primary_intent": primary_intent,
    })
    third_party_reply = door._third_party_guardrail(message, primary_intent)
    if third_party_reply:
        return third_party_reply

    facts = door.gather_customer_facts(message, customer_context)
    facts["scope_domain"] = scope.get("domain")
    preferred_reply = door._preferred_name_reply_if_requested(message, facts)
    if preferred_reply:
        return preferred_reply
    local_reply = door._local_raffle_reply(message, facts)
    if local_reply:
        return local_reply
    if door.detect_available_numbers_inquiry(message.text):
        return door.build_available_numbers_reply(message)
    print("[openai.agent] routing", {
        "mode": "openai_with_db_context",
        "primary_intent": facts.get("primary_intent"),
        "input_modality": message.input_modality,
        "text_length": len(message.text or ""),
        "has_openai_key": bool(door.get_settings().openai_api_key),
        "transcription_failed": message.transcription_failed,
    })
    return door.generate_openai_reply(message, customer_context, facts)
