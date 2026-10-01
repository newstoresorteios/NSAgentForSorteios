from __future__ import annotations

from typing import Any
import re

from app.models import AgentResult, IncomingMessage
from app.persona.site_knowledge import (
    NS_SALES_WHATSAPP,
)


from app.ops.handoff_consent import (
    CONFIRMED_REASONS, consent_reason, offer_text,
    is_handoff_acceptance, last_assistant_offered_handoff, promises_handoff,
    resolve_handoff_history,
)


def _summary_text(value, limit=300):
    text = str(value or '')
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[e-mail informado]', text)
    text = re.sub(r'(?<!\d)\d{3}[. ]?\d{3}[. ]?\d{3}[- ]?\d{2}(?!\d)', '[documento informado]', text)
    text = re.sub(r'(?<!\d)\d{2}[. ]?\d{3}[. ]?\d{3}[/ ]?\d{4}[- ]?\d{2}(?!\d)', '[documento informado]', text)
    return text[:limit]


def build_handoff_summary(incoming, result, commerce_state=None):
    """Factual internal brief; never a generated claim of a completed transfer."""
    from app.commerce.commerce_context import CommerceConversationState, checkout_fields_view
    state = CommerceConversationState.from_payload(commerce_state)
    context = state.canonical_context()
    handoff = (result.response_metadata or {}).get('handoff') or {}
    allowed = {'subject_brand', 'subject_model', 'subject_reference', 'budget_min', 'budget_max',
               'occasion', 'style', 'color', 'material', 'mechanism', 'crystal',
               'delivery_deadline_text', 'delivery_mode', 'explicit_no_preferences'}
    constraints = {key: (_summary_text(value) if isinstance(value, str) else value)
                   for key, value in state.active_preferences.items() if key in allowed}
    product = context['product_focus']
    product = {key: _summary_text(value) for key, value in product.items() if value} if product else None
    return {
        'version': 1, 'source': 'persisted_conversation_state',
        'customer_request': _summary_text(incoming.text, 500),
        'objective': state.active_goal, 'constraints': constraints,
        'product_focus': product,
        'order_focus': context['order_focus'] if state.order_status or state.order_session_id else None,
        'pending_action': state.pending_action,
        'pending_question': _summary_text(state.pending_question) or None,
        'delivery_requirement': context['delivery_requirement'],
        'known_checkout_fields': [key for key, value in checkout_fields_view(state.checkout_draft).items() if value],
        'consent': {'confirmed': handoff.get('confirmed') is True,
                    'reason': handoff.get('consent_reason')},
        'unresolved_reason': result.safety_reason,
        'inbound_id': (incoming.raw or {}).get('inbound_id'),
    }


def attach_handoff_summary(incoming, result, commerce_state=None):
    handoff = (result.response_metadata or {}).get('handoff')
    if isinstance(handoff, dict) and (handoff.get('confirmed') or handoff.get('offer')):
        handoff['summary'] = build_handoff_summary(incoming, result, commerce_state)
    return result


def _pending_offer_copy(result, commerce_state=None):
    from app.commerce.commerce_context import CommerceConversationState
    state = CommerceConversationState.from_payload(
        commerce_state or (result.response_metadata or {}).get('commerce_state'))
    prefs = state.active_preferences
    known = []
    if prefs.get('occasion'):
        known.append('ocasião: ' + _summary_text(prefs['occasion'], 80))
    if prefs.get('delivery_deadline_text'):
        known.append('prazo desejado: ' + _summary_text(prefs['delivery_deadline_text'], 100))
    acknowledgment = ('Você já informou ' + '; '.join(known) + '. ') if known else ''
    return (acknowledgment + 'Ainda não consegui concluir esta consulta com segurança. '
            'A opção de encaminhar à equipe continua disponível, se você quiser.')


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
    commerce_state=None,
) -> AgentResult:
    confirmed = consent_reason(incoming, recent_turns)
    metadata = dict(result.response_metadata or {})
    if (not confirmed and metadata.get("story_selection_pending") is True
            and not result.handoff_required):
        # Ordinary disambiguation is not a failed attendance or a transfer request.
        return result
    previous = metadata.get("handoff") if isinstance(metadata.get("handoff"), dict) else {}
    reason = result.safety_reason or previous.get("reason") or confirmed
    from app.ops.failure_explanation import apply_failure_explanation, failure_explanation
    failure = failure_explanation(result)
    proposed = bool(failure or result.handoff_required or previous.get("required") or previous.get("offer")
                    or result.safety_reason in _INTEGRATION_HANDOFF_REASONS or promises_handoff(result.reply_text))
    if not proposed and not confirmed:
        return result
    if confirmed:
        from app.configuration.runtime import message
        result.reply_text = message("handoff_requested")
    elif (metadata.get('response_source') == 'ready_delivery_storefront' and metadata.get('ready_delivery_check')
          or metadata.get('response_source') == 'order_delivery_status' and (result.commercial_data or {}).get('order_id')):
        # This bounded lookup already explains its evidence and offers help.
        # A handoff offer must not erase the requested list or outage explanation.
        pass
    elif last_assistant_offered_handoff(resolve_handoff_history(incoming, recent_turns)):
        # Repeated failure must not erase the customer's constraints or ask the
        # same transfer question. A fresh affirmative answer still grants consent.
        result.reply_text = _pending_offer_copy(result, commerce_state)
        metadata['handoff_offer_repeated'] = True
    else:
        # Replace any premature transfer promise produced by tools, policies or validators.
        if failure:
            result = apply_failure_explanation(result)
            result.reply_text = f"{result.reply_text}\n\n{offer_text()}"
        else:
            result.reply_text = offer_text()
    result.handoff_required = bool(confirmed)
    result.intent = "handoff"
    result.safety_reason = reason or "human_review_needed"
    metadata.update(result.response_metadata)
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
    if commerce_state is not None or not previous.get('summary'):
        attach_handoff_summary(incoming, result, commerce_state)
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
    handoff = build_human_handoff_result(
        reason=f"integration_failure:{reason}",
        reply_text=None,
    )
    from app.ops.failure_explanation import apply_failure_explanation
    handoff = apply_failure_explanation(handoff)
    handoff.reply_text = f"{handoff.reply_text}\n\n{offer_text()}"
    handoff.response_metadata = {**result.response_metadata, **handoff.response_metadata}
    if handoff.response_metadata.get("agent_disclosure"):
        from app.identity.agent_disclosure import apply_agent_disclosure
        handoff = apply_agent_disclosure(handoff)
    return handoff


def ensure_handoff_queued(incoming: IncomingMessage, result: AgentResult) -> bool:
    """Never deliver a transfer confirmation before the tenant-scoped queue write."""
    payload = handoff_provider_payload(result)
    if payload is None:
        return not result.handoff_required
    from app.ops.handoff_queue import mark_conversa_for_human_handoff
    return bool(mark_conversa_for_human_handoff(incoming, reason=payload["consent_reason"]))


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
        "summary": handoff.get("summary"),
    }
