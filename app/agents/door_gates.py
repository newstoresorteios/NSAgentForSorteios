"""Early door exits: block, handoff, audio, farewell.

Look up patched names on ``app.agents.door`` at call time so tests that
monkeypatch ``openai_agent.detect_blocked_request`` keep working.
"""

from __future__ import annotations

from typing import Any

from app.models import AgentResult, IncomingMessage


def _door():
    import app.agents.door as door_mod

    return door_mod


def try_entry_gates(message: IncomingMessage) -> AgentResult | None:
    door = _door()
    blocked_reason = door.detect_blocked_request(message.text)
    if blocked_reason:
        return door._annotate_agent_result(
            door.build_human_handoff_result(reason=blocked_reason),
            domain="guardrail",
            response_source="guardrail",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    human_reason = door.should_request_human_handoff(message)
    if human_reason:
        return door._annotate_agent_result(
            door.build_human_handoff_result(reason=human_reason),
            domain="guardrail",
            response_source="handoff",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    from app.channels.audio_service import (
        audio_transcription_failed_result,
        inbound_audio_failed,
    )

    if inbound_audio_failed(message):
        return door._annotate_agent_result(
            audio_transcription_failed_result(),
            domain="technical_fallback",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="audio_transcription_failed",
        )
    return None


def try_accepted_handoff(
    message: IncomingMessage,
    recovery_turns: list[dict[str, Any]] | None,
) -> AgentResult | None:
    door = _door()
    accepted = door.should_request_human_handoff(
        message,
        recent_turns=recovery_turns,
    )
    if accepted != "customer_accepted_handoff_offer":
        return None
    return door._annotate_agent_result(
        door.build_human_handoff_result(reason=accepted),
        domain="guardrail",
        response_source="handoff",
        used_openai_interpreter=False,
        used_openai_responder=False,
        used_tray=False,
    )


def try_farewell(
    message: IncomingMessage,
    customer_context: dict,
    commerce_state: Any,
) -> AgentResult | None:
    door = _door()
    if not door.is_farewell_message(message.text):
        return None
    checkout_name = None
    try:
        draft = getattr(commerce_state, "checkout_draft", None)
        customer = getattr(draft, "customer", None) if draft is not None else None
        checkout_name = getattr(customer, "name", None)
    except Exception:
        checkout_name = None
    display_name = door.resolve_address_name(
        preferred_name=(customer_context or {}).get("preferred_name")
        if isinstance(customer_context, dict)
        else None,
        checkout_name=checkout_name,
        account_name=(customer_context or {}).get("display_name")
        or (customer_context or {}).get("name")
        if isinstance(customer_context, dict)
        else None,
        whatsapp_profile_name=message.sender_name,
    )
    return door._annotate_agent_result(
        AgentResult(
            reply_text=door.choose_farewell_reply(display_name),
            intent="general",
            handoff_required=False,
        ),
        domain="greeting",
        response_source="farewell",
        used_openai_interpreter=False,
        used_openai_responder=False,
        used_tray=False,
    )
