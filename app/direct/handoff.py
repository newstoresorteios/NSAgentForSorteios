"""Keep transfer claims aligned with recorded customer consent."""
from __future__ import annotations

import re

from app.configuration.runtime import message
from app.ops.handoff_consent import consent_reason, offer_text, promises_handoff


def _without_transfer_promise(reply: str) -> str:
    kept = [
        part for part in re.split(r"(?<=[.!?])\s+", (reply or "").strip())
        if part and not promises_handoff(part)
    ]
    body = " ".join(kept).strip()
    offer = offer_text()
    if body and not promises_handoff(body):
        return f"{body}\n\n{offer}"
    return offer


def settle_handoff(reply: str, incoming, history, confirmed: dict | None):
    """A spoken transfer is delivered only after consent is already recorded."""
    if confirmed:
        return reply, confirmed
    reason = consent_reason(incoming, history)
    if reason:
        return message("handoff_requested"), {
            "required": True,
            "confirmed": True,
            "offer": False,
            "consent_reason": reason,
            "reason": reason,
            "provider_action": "mark_for_human",
            "summary": "customer_consent",
        }
    if not promises_handoff(reply):
        return reply, None
    return _without_transfer_promise(reply), {
        "required": False,
        "offer": True,
        "confirmed": False,
        "reason": "unconfirmed_transfer_promise",
        "provider_action": "await_customer_confirmation",
    }
