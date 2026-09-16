from __future__ import annotations

from typing import Any

from app.models import AgentResult, IncomingMessage
from app.persona.site_knowledge import (
    NS_SALES_WHATSAPP,
)


from app.ops.handoff_consent import (
    CONFIRMED_REASONS, consent_reason, offer_text,
    is_handoff_acceptance, last_assistant_offered_handoff, promises_handoff,
)


def should_request_human_handoff(
    incoming: IncomingMessage,
    *,
    result: AgentResult | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> str | None:
    confirmed = consent_reason(incoming, recent_turns)
    if confirmed:
        return confirmed
    if result is not None and result.handoff_required:
        return result.safety_reason or "handoff_required"
    from app.verify.guardrails import detect_trade_in_or_appraisal_request

    if detect_trade_in_or_appraisal_request(incoming.text):
        return "trade_in_or_appraisal"
    return None


def build_human_handoff_result(
    *,
    reason: str,
    reply_text: str | None = None,
) -> AgentResult:
    confirmed = reason in CONFIRMED_REASONS
    if confirmed:
        from app.configuration.runtime import message
        text = message("handoff_requested")
    else:
        text = offer_text()
    return AgentResult(
        reply_text=text,
        intent="handoff",
        handoff_required=confirmed,
        safety_reason=reason,
        response_metadata={
            "domain": "guardrail",
            "response_source": "handoff",
            "handoff": {
                "required": confirmed,
                "offer": not confirmed,
                "confirmed": confirmed,
                "consent_reason": reason if confirmed else None,
                "reason": reason,
                "contact_whatsapp": NS_SALES_WHATSAPP(),
                "provider_action": "mark_for_human" if confirmed else "await_customer_confirmation",
            },
        },
    )


def enrich_handoff_metadata(
    incoming: IncomingMessage,
    result: AgentResult,
    *,
    recent_turns: list[dict[str, Any]] | None = None,
) -> AgentResult:
    confirmed = consent_reason(incoming, recent_turns)
    metadata = dict(result.response_metadata or {})
    previous = metadata.get("handoff") if isinstance(metadata.get("handoff"), dict) else {}
    reason = result.safety_reason or previous.get("reason") or confirmed
    proposed = bool(result.handoff_required or previous.get("required") or previous.get("offer")
                    or result.safety_reason in _INTEGRATION_HANDOFF_REASONS or promises_handoff(result.reply_text))
    if not proposed and not confirmed:
        return result
    if confirmed:
        from app.configuration.runtime import message
        result.reply_text = message("handoff_requested")
    else:
        # Replace any premature transfer promise produced by tools, policies or validators.
        result.reply_text = offer_text()
    result.handoff_required = bool(confirmed)
    result.intent = "handoff"
    result.safety_reason = reason or "human_review_needed"
    metadata["handoff"] = {
        **previous,
        "required": bool(confirmed), "offer": not bool(confirmed),
        "confirmed": bool(confirmed), "consent_reason": confirmed,
        "reason": result.safety_reason,
        "channel": incoming.channel,
        "conversation_id_present": bool(incoming.conversation_id),
        "visitor_id_present": bool(incoming.visitor_id),
        "contact_whatsapp": NS_SALES_WHATSAPP(),
        "provider_action": "mark_for_human" if confirmed else "await_customer_confirmation",
    }
    metadata.setdefault("domain", "guardrail")
    result.response_metadata = metadata
    return result


_INTEGRATION_HANDOFF_REASONS = frozenset(
    {
        "category_adapter_error",
        "tray_authentication_failed",
        "tray_connection_failed",
    }
)


def apply_integration_failure_handoff(result: AgentResult) -> AgentResult:
    """A failed integration offers human help and waits for customer consent."""
    if result.handoff_required or (result.response_metadata or {}).get("handoff", {}).get("offer"):
        return result
    reason = (result.safety_reason or "").strip()
    if reason not in _INTEGRATION_HANDOFF_REASONS:
        return result
    return build_human_handoff_result(
        reason=f"integration_failure:{reason}",
        reply_text=None,
    )


def handoff_provider_payload(result: AgentResult) -> dict[str, Any] | None:
    handoff = (result.response_metadata or {}).get("handoff")
    if (not isinstance(handoff, dict) or not result.handoff_required
            or not handoff.get("required") or handoff.get("confirmed") is not True
            or handoff.get("consent_reason") not in CONFIRMED_REASONS):
        return None
    return {
        "required": True,
        "consent_reason": handoff["consent_reason"],
        "reason": handoff.get("reason"),
        "provider_action": handoff.get("provider_action"),
        "contact_whatsapp": handoff.get("contact_whatsapp"),
    }
