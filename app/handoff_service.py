from __future__ import annotations

from typing import Any

from .guardrails import default_safe_handoff, detect_human_support_request
from .models import AgentResult, IncomingMessage
from .site_knowledge import HUMAN_SUPPORT_MESSAGE, NS_SALES_WHATSAPP, TRADE_IN_HANDOFF_MESSAGE


_HANDOFF_OFFER_MARKERS = (
    "passar para o joão",
    "passar para o joao",
    "passar pra o joão",
    "passar pra o joao",
    "coloco com o joão",
    "coloco com o joao",
    "encaminhar",
    "encaminho",
    "posso encaminhar",
    "encaminhar para a equipe",
    "quer que eu encaminhe",
    "equipe da new store",
    "joão da equipe",
    "joao da equipe",
    "atendente da loja",
    "falar com a equipe",
)

_HANDOFF_ACCEPT_PHRASES = {
    "sim",
    "si",
    "yes",
    "ok",
    "okay",
    "certo",
    "beleza",
    "blz",
    "pode",
    "pode ser",
    "isso",
    "uhum",
    "uhu",
    "por favor",
    "pf",
    "pfv",
    "faz favor",
    "manda",
    "pode mandar",
    "pode encaminhar",
    "encaminha",
    "encaminhe",
    "quero",
    "quero sim",
    "sim por favor",
    "pode por favor",
}


def _fold_handoff(text: str | None) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).split()
    )


def last_assistant_offered_handoff(recent_turns: list[dict[str, Any]] | None) -> bool:
    for turn in reversed(recent_turns or []):
        if turn.get("role") != "assistant":
            continue
        folded = _fold_handoff(str(turn.get("content") or ""))
        if any(marker in folded for marker in _HANDOFF_OFFER_MARKERS):
            return True
        metadata = turn.get("metadata") if isinstance(turn.get("metadata"), dict) else {}
        handoff = metadata.get("handoff")
        if isinstance(handoff, dict) and (
            handoff.get("offer") or handoff.get("required")
        ):
            return True
        if metadata.get("handoff_required") or (
            isinstance(metadata.get("handoff"), dict)
            and metadata["handoff"].get("required")
        ):
            return True
        break
    return False


def is_handoff_acceptance(
    text: str | None,
    recent_turns: list[dict[str, Any]] | None,
) -> bool:
    if not last_assistant_offered_handoff(recent_turns):
        return False
    folded = _fold_handoff(text).strip("!?.,")
    if folded in _HANDOFF_ACCEPT_PHRASES:
        return True
    return folded.startswith("por favor") or folded.startswith("sim ")


def should_request_human_handoff(
    incoming: IncomingMessage,
    *,
    result: AgentResult | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> str | None:
    if result is not None and result.handoff_required:
        return result.safety_reason or "handoff_required"
    if detect_human_support_request(incoming.text):
        return "customer_requested_human"
    if is_handoff_acceptance(incoming.text, recent_turns):
        return "customer_accepted_handoff_offer"
    from .guardrails import detect_trade_in_or_appraisal_request

    if detect_trade_in_or_appraisal_request(incoming.text):
        return "trade_in_or_appraisal"
    return None


def build_human_handoff_result(
    *,
    reason: str,
    reply_text: str | None = None,
) -> AgentResult:
    text = (reply_text or "").strip()
    if not text:
        if reason == "trade_in_or_appraisal":
            text = TRADE_IN_HANDOFF_MESSAGE
        elif reason == "customer_accepted_handoff_offer":
            text = (
                "Perfeito — vou encaminhar seu atendimento para o João da equipe. "
                f"{HUMAN_SUPPORT_MESSAGE}"
            )
        else:
            text = (
                "Vou encaminhar seu atendimento para a equipe da New Store. "
                f"{HUMAN_SUPPORT_MESSAGE}"
            )
    if reason.startswith("blocked_topic:"):
        text = default_safe_handoff()
    return AgentResult(
        reply_text=text,
        intent="handoff",
        handoff_required=True,
        safety_reason=reason,
        response_metadata={
            "domain": "guardrail",
            "response_source": "handoff",
            "handoff": {
                "required": True,
                "reason": reason,
                "contact_whatsapp": NS_SALES_WHATSAPP,
                "provider_action": "mark_for_human",
            },
        },
    )


def enrich_handoff_metadata(
    incoming: IncomingMessage,
    result: AgentResult,
) -> AgentResult:
    reason = should_request_human_handoff(incoming, result=result)
    if not reason:
        return result
    result.handoff_required = True
    result.safety_reason = result.safety_reason or reason
    handoff = result.response_metadata.get("handoff")
    if not isinstance(handoff, dict):
        handoff = {}
    handoff.update(
        {
            "required": True,
            "reason": reason,
            "channel": incoming.channel,
            "conversation_id_present": bool(incoming.conversation_id),
            "visitor_id_present": bool(incoming.visitor_id),
            "contact_whatsapp": NS_SALES_WHATSAPP,
            "provider_action": "mark_for_human",
        }
    )
    result.response_metadata["handoff"] = handoff
    result.response_metadata.setdefault("domain", "guardrail")
    return result


def handoff_provider_payload(result: AgentResult) -> dict[str, Any] | None:
    handoff = (result.response_metadata or {}).get("handoff")
    if not isinstance(handoff, dict) or not handoff.get("required"):
        return None
    return {
        "required": True,
        "reason": handoff.get("reason"),
        "provider_action": handoff.get("provider_action"),
        "contact_whatsapp": handoff.get("contact_whatsapp"),
    }
