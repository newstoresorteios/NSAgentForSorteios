"""Customer consent for human attendance, scoped to the current delivered dialogue."""
from __future__ import annotations

import logging
import json
import re
import unicodedata

from app.core.turn_cache import cached_turn_read

CONFIRMED_REASONS = frozenset({"customer_requested_human", "customer_accepted_handoff_offer"})


def consent_rules():
    from app.configuration.runtime import policy
    return json.loads(policy('handoffConsentRules'))


def fold(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())


def customer_requests_human(text: str | None) -> bool:
    text = fold(text)
    rules = consent_rules()
    if re.search(rules['negative'], text):
        return False
    # Natural statements of necessity are already an explicit request.  They
    # must not be downgraded to a second confirmation question.
    if re.search(
        r"\b(?:tenho|terei|vou ter|preciso)\s+(?:mesmo\s+)?que\s+"
        r"(?:falar|conversar)\s+com\s+(?:um |uma |o |a )?"
        r"(?:atendente|humano|ser humano|pessoa|equipe|vendedor|vendas)\b",
        text,
    ):
        return True
    return any(re.fullmatch(p,text) for p in rules['directFull']) or any(re.search(p,text) for p in rules['directSearch'])


def acceptance_text(text: str | None) -> bool:
    text = re.sub(r"[!?.,;]", " ", fold(text))
    text = " ".join(text.split())
    return bool(re.fullmatch(consent_rules()['acceptance'], text))


def last_assistant_offered_handoff(turns: list[dict] | None) -> bool:
    last = next((turn for turn in reversed(turns or []) if turn.get("role") in {"assistant", "user"}), None)
    if not last or last.get("role") != "assistant":
        return False
    metadata = last.get("metadata") if isinstance(last.get("metadata"), dict) else {}
    handoff = metadata.get("handoff") if isinstance(metadata.get("handoff"), dict) else {}
    if handoff.get("offer") is True and handoff.get("required") is not True:
        return True
    text = fold(last.get("content"))
    # Mentioning the team or forwarding a product link is not a transfer offer.
    return all(re.search(pattern,text) for pattern in consent_rules()['offerAll'])


def is_handoff_acceptance(text: str | None, recent_turns: list[dict] | None) -> bool:
    return acceptance_text(text) and last_assistant_offered_handoff(recent_turns)


def is_handoff_decline(text: str | None, recent_turns: list[dict] | None) -> bool:
    normalized=' '.join(re.sub(r'[!?.,;]',' ',fold(text)).split())
    return bool(re.fullmatch(consent_rules()['decline'],normalized)
                and last_assistant_offered_handoff(recent_turns))


def promises_handoff(text: str | None) -> bool:
    return bool(re.search(consent_rules()['transferPromise'],fold(text)))


@cached_turn_read
def delivered_handoff_turns(inbound_id: int, conversation_id: str, channel: str) -> list[dict]:
    from app.evaluation.context import current_evaluation
    evaluation = current_evaluation()
    if evaluation is not None:
        return evaluation.history
    from app.config import get_settings
    if not get_settings().database_url:
        return []
    from app.db import get_conn
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT previous.text, response.reply_text, response.response_metadata
                FROM public.ai_inbound_messages current
                JOIN LATERAL (
                    SELECT i.* FROM public.ai_inbound_messages i
                    WHERE i.workspace_id=current.workspace_id AND i.channel=current.channel
                      AND i.conversation_id=current.conversation_id AND i.id<current.id
                    ORDER BY i.id DESC LIMIT 1
                ) previous ON true
                LEFT JOIN LATERAL (
                    SELECT r.reply_text,r.provider_response->'_agent_metadata' AS response_metadata FROM public.ai_agent_responses r
                    WHERE r.inbound_id=previous.id AND r.workspace_id=current.workspace_id
                      AND r.channel=current.channel AND r.provider_send_ok=true
                    ORDER BY r.id DESC LIMIT 1
                ) response ON true
                WHERE current.id=%s AND current.conversation_id=%s AND current.channel=%s
                  AND current.workspace_id IS NOT NULL
            """, (inbound_id, conversation_id, channel))
            row = cur.fetchone()
        if not row:
            return []
        turns = [{"role": "user", "content": row.get("text") or ""}]
        if row.get("reply_text"):
            turns.append({"role": "assistant", "content": row["reply_text"], "metadata": row.get("response_metadata") or {}})
        return turns
    except Exception as exc:
        logging.getLogger(__name__).warning("Handoff consent lookup unavailable: %s", type(exc).__name__)
        return []


def consent_reason(incoming, recent_turns: list[dict] | None = None) -> str | None:
    if customer_requests_human(incoming.text):
        return "customer_requested_human"
    if not acceptance_text(incoming.text):
        return None
    if recent_turns is None:
        from app.evaluation.context import current_evaluation
        evaluation = current_evaluation()
        if evaluation is not None:
            recent_turns = evaluation.history
        else:
            inbound_id = (incoming.raw or {}).get("inbound_id")
            if inbound_id is None or not incoming.conversation_id:
                return None
            recent_turns = delivered_handoff_turns(inbound_id, incoming.conversation_id, incoming.channel)
    if is_handoff_acceptance(incoming.text, recent_turns):
        return "customer_accepted_handoff_offer"
    return None


def offer_text() -> str:
    from app.configuration.runtime import message
    return message('handoff_offer')
