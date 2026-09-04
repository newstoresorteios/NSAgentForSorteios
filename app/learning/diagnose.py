"""Deterministic attendance diagnosis from stored metadata + reply heuristics."""

from __future__ import annotations

import json
import re
from typing import Any

from app.identity.greeting_policy import is_generic_greeting_reply
from app.verify.guardrails import detect_trade_in_or_appraisal_request


_EMPTY_CATALOG_RE = re.compile(
    r"n[aã]o encontrei (esse produto|op[cç][oõ]es)",
    flags=re.IGNORECASE,
)
_TRADE_BUY_CLAIM_RE = re.compile(
    r"(avaliamos|trocamos|compramos).{0,60}(seminovo|usado|particulares)|"
    r"avalia, troca e compra",
    flags=re.IGNORECASE,
)

PIPELINE_CAPTURE_REASONS = frozenset({
    "commerce_clarification",
    "scope_send_gate_blocked",
    "answer_council_blocked",
    "recommendation_budget_miss",
})

_COUNCIL_ISSUE_CODES = frozenset({
    "ignored_model",
    "fact_model_mismatch",
    "greeting_steal",
    "constraint_miss",
})

_HIGH_SIGNAL_CODES = frozenset({
    "ignored_model",
    "fact_model_mismatch",
    "answer_council_blocked",
    "recommendation_budget_miss",
    "scope_send_gate_blocked",
})

_INSIGHT_CATEGORIES = {
    "preference_misread": "retrieval",
    "empty_catalog": "retrieval",
    "trade_in_policy_miss": "policy",
    "critique_failed": "persona",
    "commerce_clarification": "retrieval",
    "scope_send_gate_blocked": "retrieval",
    "answer_council_blocked": "retrieval",
    "recommendation_budget_miss": "retrieval",
    "ignored_model": "retrieval",
    "fact_model_mismatch": "retrieval",
    "greeting_steal": "greeting",
}


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def group_by_conversation(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(
            row.get("conversation_id")
            or row.get("sender_key")
            or row.get("response_id")
            or "unknown"
        )
        grouped.setdefault(key, []).append(row)
    return grouped


def classify_attendance(row: dict[str, Any]) -> dict[str, Any]:
    customer = str(row.get("customer_text") or "")
    reply = str(row.get("agent_reply") or "")
    metadata = _parse_metadata(row.get("response_metadata"))
    failure_codes: list[str] = []
    outcome = "success"

    if row.get("handoff_required"):
        outcome = "handoff"
    if _EMPTY_CATALOG_RE.search(reply):
        failure_codes.append("empty_catalog")
        outcome = "empty_catalog"
        folded = customer.lower()
        if any(token in folded for token in ("feminino", "masculino", "até", "ate", "reais")):
            failure_codes.append("preference_misread")
    if (
        is_generic_greeting_reply(reply)
        and customer.strip()
        and not is_generic_greeting_reply(customer)
    ):
        failure_codes.append("greeting_steal")
        if outcome == "success":
            outcome = "duplicate_greeting"
    if detect_trade_in_or_appraisal_request(customer):
        if _TRADE_BUY_CLAIM_RE.search(reply):
            failure_codes.append("trade_in_policy_miss")
            outcome = "policy_miss"
    critique = metadata.get("response_critique") if isinstance(metadata, dict) else None
    if isinstance(critique, dict) and critique.get("approved") is False:
        failure_codes.append("critique_failed")
        outcome = "failure"
    council = metadata.get("answer_council") if isinstance(metadata, dict) else None
    if isinstance(council, dict):
        issues = council.get("issues") or []
        if isinstance(issues, list):
            for issue in issues:
                code = str(issue or "").strip()
                if code:
                    failure_codes.append(code)
                    if code in _COUNCIL_ISSUE_CODES and outcome in {"success", "handoff"}:
                        outcome = "failure"
    safety_reason = str(row.get("safety_reason") or "").strip()
    if safety_reason == "commerce_clarification":
        failure_codes.append("commerce_clarification")
        if outcome == "success":
            outcome = "unclear"
    elif safety_reason == "scope_send_gate_blocked":
        failure_codes.append("scope_send_gate_blocked")
        outcome = "failure"
        gate = metadata.get("scope_send_gate") if isinstance(metadata, dict) else None
        if isinstance(gate, dict) and gate.get("reason"):
            failure_codes.append(str(gate["reason"]))
    elif safety_reason == "answer_council_blocked":
        failure_codes.append("answer_council_blocked")
        outcome = "failure"
    elif safety_reason == "recommendation_budget_miss":
        failure_codes.append("recommendation_budget_miss")
        outcome = "failure"
    # Dedupe while preserving order.
    seen: set[str] = set()
    unique_codes: list[str] = []
    for code in failure_codes:
        if code in seen:
            continue
        seen.add(code)
        unique_codes.append(code)
    if unique_codes and outcome == "success":
        outcome = "failure"
    return {
        "outcome": outcome,
        "failure_codes": unique_codes,
        "signals": {
            "intent": row.get("intent"),
            "safety_reason": row.get("safety_reason"),
            "channel": row.get("channel"),
        },
    }


def classify_pipeline_block(
    *,
    safety_reason: str,
    result_metadata: dict[str, Any] | None = None,
    intent: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    reason = str(safety_reason or "").strip()
    metadata = dict(result_metadata or {})
    failure_codes = [reason] if reason else []
    outcome = "unclear"
    if reason in {
        "scope_send_gate_blocked",
        "answer_council_blocked",
        "recommendation_budget_miss",
    }:
        outcome = "failure"
        gate = metadata.get("scope_send_gate")
        if isinstance(gate, dict) and gate.get("reason"):
            failure_codes.append(str(gate["reason"]))
        council = metadata.get("answer_council")
        if isinstance(council, dict):
            issues = council.get("issues") or []
            if isinstance(issues, list):
                for issue in issues:
                    code = str(issue or "").strip()
                    if code and code not in failure_codes:
                        failure_codes.append(code)
    elif reason == "commerce_clarification":
        outcome = "unclear"
    return {
        "outcome": outcome,
        "failure_codes": failure_codes,
        "signals": {
            "intent": intent,
            "safety_reason": reason,
            "channel": channel,
            "capture_source": "pipeline",
        },
    }


def cluster_is_high_signal(code: str, evidence: int) -> bool:
    if evidence >= 2:
        return True
    return str(code) in _HIGH_SIGNAL_CODES


def insight_category_for(code: str) -> str:
    return _INSIGHT_CATEGORIES.get(str(code), "other")


def aggregate_failures(reviews: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        for code in review.get("failure_codes") or []:
            buckets.setdefault(str(code), []).append(review)
    return buckets
