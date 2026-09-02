from __future__ import annotations

import json
import re
from typing import Any, Literal

from openai import APIError
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .models import AgentResult, IncomingMessage
from .runtime_context import get_current_turn
from .turn_runtime import LLMCallBudgetExceeded


_MONEY_RE = re.compile(r"R\$\s*\d", flags=re.IGNORECASE)
_STOCK_RE = re.compile(
    r"\b(estoque|dispon[ií]vel|esgotado)\b",
    flags=re.IGNORECASE,
)
_PROMO_RE = re.compile(
    r"\b(promo[cç][aã]o|desconto|oferta)\b",
    flags=re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://", flags=re.IGNORECASE)
_THANKS_RE = re.compile(
    r"^\s*(obrigad[oa]|valeu|thanks|thank you)[!.,\s]*$",
    flags=re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r"^\s*(oi|ol[aá]|bom dia|boa tarde|boa noite|hello|hi)[!.,\s]*$",
    flags=re.IGNORECASE,
)

_LOW_RISK_SOURCES = {
    "local_greeting",
    "context_resume_soft",
    "handoff",
    "guardrail",
    "out_of_scope",
    "local_raffle",
}


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(default=100, ge=0, le=100)
    pass_check: bool = True
    issues: list[str] = Field(default_factory=list)
    summary: str = ""

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from .openai_strict_schema import apply_openai_strict_schema

        schema = handler(core_schema)
        return apply_openai_strict_schema(schema)


class JudgeReport(BaseModel):
    triggered: bool = False
    mode: Literal["off", "shadow", "enforce"] = "off"
    reason: str | None = None
    signals: list[str] = Field(default_factory=list)
    verdict: JudgeVerdict | None = None
    applied: bool = False
    skipped_reason: str | None = None


def _response_source(result: AgentResult) -> str:
    return str((result.response_metadata or {}).get("response_source") or "")


def is_low_risk_judge_skip(
    incoming: IncomingMessage | None,
    result: AgentResult | None,
) -> tuple[bool, str | None]:
    """Paths that must not spend a judge LLM call (Phase 9)."""
    if result is None:
        return False, None
    if result.handoff_required:
        return True, "human_handoff"
    source = _response_source(result)
    if source in _LOW_RISK_SOURCES:
        return True, f"deterministic:{source}"
    text = ((incoming.text if incoming else "") or "").strip()
    if result.intent == "greeting" and not (result.commercial_data or {}):
        return True, "greeting_intent"
    if text and _GREETING_RE.match(text):
        return True, "soft_greeting"
    if text and _THANKS_RE.match(text):
        return True, "thanks"
    if result.intent in {"out_of_scope", "handoff"} and not (
        result.commercial_data or {}
    ):
        return True, "non_commercial_intent"
    # Deterministic reply with no commercial claims/facts.
    if (
        source.startswith("deterministic")
        or source.startswith("local_")
        or source in {"context_resume", "technical_fallback"}
    ):
        reply = result.reply_text or ""
        has_commercial_signal = bool(
            _MONEY_RE.search(reply)
            or _URL_RE.search(reply)
            or (result.commercial_data or {})
        )
        if not has_commercial_signal:
            return True, "deterministic_without_commercial_facts"
    return False, None


def collect_judge_risk_signals(
    *,
    result: AgentResult,
    risk_score: int,
    factual_valid: bool,
    openai_call_count: int,
    threshold: int = 70,
) -> list[str]:
    signals: list[str] = []
    metadata = result.response_metadata or {}
    reply = result.reply_text or ""
    commercial = result.commercial_data or {}
    validation = metadata.get("factual_validation") or {}

    if not factual_valid or validation.get("valid") is False:
        signals.append("factual_validation_failed")
    if risk_score >= threshold:
        signals.append("high_risk_score")
    if openai_call_count >= 3:
        signals.append("complex_llm_turn")
    if _MONEY_RE.search(reply):
        signals.append("reply_contains_price")
    if _STOCK_RE.search(reply):
        signals.append("reply_contains_stock")
    if _PROMO_RE.search(reply):
        signals.append("reply_contains_promo")
    if _URL_RE.search(reply):
        if "payment" in reply.casefold() or commercial.get("payment"):
            signals.append("reply_contains_payment_link")
        else:
            signals.append("reply_contains_commercial_link")
    products = commercial.get("products")
    if (
        (isinstance(products, list) and products)
        or metadata.get("presented_products")
        or commercial.get("product_id")
        or metadata.get("active_product")
    ):
        signals.append("commerce_products_presented")
    sku_bits = [
        commercial.get("sku"),
        commercial.get("reference"),
        commercial.get("ean"),
        metadata.get("sku"),
        metadata.get("reference"),
        metadata.get("ean"),
    ]
    if isinstance(products, list):
        for product in products[:5]:
            if not isinstance(product, dict):
                continue
            sku_bits.extend(
                (
                    product.get("sku"),
                    product.get("reference"),
                    product.get("ean"),
                )
            )
    if any(str(bit or "").strip() for bit in sku_bits):
        signals.append("commerce_sku_or_reference")
    if commercial.get("order_id") or metadata.get("order_state"):
        pending = str(metadata.get("pending_action") or "")
        if "confirm" in pending or metadata.get("order_created"):
            signals.append("order_prepared_or_confirmed")
        elif commercial.get("payment") or metadata.get("payment_state"):
            signals.append("payment_consulted")
        else:
            signals.append("order_context_present")
    if metadata.get("used_tray") and metadata.get("fallback_reason"):
        signals.append("fallback_after_partial_tooling")
    if result.safety_reason and "fail" in str(result.safety_reason).casefold():
        signals.append("tool_or_safety_failure")
    if validation.get("conflicting_claims"):
        signals.append("conflicting_tool_or_fact_claims")
    if validation.get("unsupported_claims") or validation.get("missing_evidence"):
        signals.append("unsupported_or_missing_evidence")
    if metadata.get("side_effect") or pending_side_effect(metadata):
        signals.append("side_effect_action")
    confidence = metadata.get("interpretation_confidence")
    if confidence is not None:
        try:
            if float(confidence) < 0.45:
                signals.append("low_interpretation_confidence")
        except (TypeError, ValueError):
            pass
    return signals


def pending_side_effect(metadata: dict[str, Any]) -> bool:
    pending = str(metadata.get("pending_action") or "")
    return any(
        token in pending
        for token in (
            "confirm",
            "create",
            "checkout",
            "payment",
            "shipping",
            "order",
        )
    )


def should_trigger_judge(
    *,
    risk_score: int,
    factual_valid: bool,
    handoff_required: bool,
    openai_call_count: int,
    threshold: int = 70,
    incoming: IncomingMessage | None = None,
    result: AgentResult | None = None,
) -> tuple[bool, str | None]:
    # Compatibility: older callers only pass scalars.
    if result is None:
        result = AgentResult(
            reply_text="",
            intent="general",
            handoff_required=handoff_required,
        )
    skip, skip_reason = is_low_risk_judge_skip(incoming, result)
    if skip:
        return False, skip_reason
    signals = collect_judge_risk_signals(
        result=result,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
        threshold=threshold,
    )
    if not signals:
        return False, None
    return True, signals[0]


def _judge_evidence_payload(result: AgentResult) -> dict[str, Any]:
    metadata = result.response_metadata or {}
    validation = metadata.get("factual_validation") or {}
    evidence = metadata.get("fact_evidence") or []
    return {
        "fact_evidence": evidence[:20],
        "factual_validation": {
            "valid": validation.get("valid"),
            "risk_level": validation.get("risk_level"),
            "unsupported_claims": validation.get("unsupported_claims") or [],
            "conflicting_claims": validation.get("conflicting_claims") or [],
            "missing_evidence": validation.get("missing_evidence") or [],
            "evidence_sources": validation.get("evidence_sources") or [],
        },
        "commerce_flags": {
            "used_tray": bool(metadata.get("used_tray")),
            "response_source": metadata.get("response_source"),
            "pending_action": metadata.get("pending_action"),
            "fallback_reason": metadata.get("fallback_reason"),
        },
        "commercial_data_keys": sorted((result.commercial_data or {}).keys()),
    }


def _judge_must_fail_closed(result: AgentResult) -> bool:
    """Do not pass a constrained catalog turn when the judge call itself failed."""
    meta = result.response_metadata or {}
    prefs = meta.get("active_preferences") or {}
    if not isinstance(prefs, dict):
        prefs = {}
    if meta.get("hard_budget_max") is not None:
        return True
    if prefs.get("locked_identity") or prefs.get("color") or prefs.get("budget_max"):
        return True
    products = (result.commercial_data or {}).get("products") or []
    return bool(products and (prefs.get("color") or prefs.get("budget_max")))


def _failed_open_verdict(exc: BaseException, *, fail_closed: bool) -> JudgeVerdict:
    return JudgeVerdict(
        score=40,
        pass_check=not fail_closed,
        issues=[f"judge_failed:{type(exc).__name__}"],
        summary=(
            "Judge failed closed because the turn has sku/color/budget constraints."
            if fail_closed
            else "Judge failed open; shadow/off keeps original reply."
        ),
    )


async def run_quality_judge(
    incoming: IncomingMessage,
    result: AgentResult,
    *,
    mode: Literal["off", "shadow", "enforce"] = "off",
    risk_score: int = 0,
    factual_valid: bool = True,
    openai_call_count: int = 0,
) -> JudgeReport:
    report = JudgeReport(mode=mode)
    if mode == "off":
        return report

    settings = get_settings()
    threshold = int(
        getattr(settings, "agent_quality_judge_risk_threshold", 70)
    )
    skip, skip_reason = is_low_risk_judge_skip(incoming, result)
    if skip:
        report.triggered = False
        report.skipped_reason = skip_reason
        report.reason = skip_reason
        runtime = get_current_turn()
        if runtime is not None:
            runtime.judge_mode = mode
            runtime.judge_triggered = False
        return report

    signals = collect_judge_risk_signals(
        result=result,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
        threshold=threshold,
    )
    triggered = bool(signals)
    report.triggered = triggered
    report.signals = signals
    report.reason = signals[0] if signals else None
    runtime = get_current_turn()
    if runtime is not None:
        runtime.judge_mode = mode
        runtime.judge_triggered = triggered
    if not triggered:
        return report

    if not settings.openai_api_key:
        fail_closed = _judge_must_fail_closed(result)
        report.verdict = JudgeVerdict(
            score=50,
            pass_check=not fail_closed,
            issues=["openai_unavailable"],
            summary=(
                "Judge unavailable on a constrained catalog turn."
                if fail_closed
                else "Judge skipped because OpenAI is unavailable."
            ),
        )
        return report

    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=JudgeVerdict,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Avalie a qualidade e segurança da resposta do agente. "
                        "Use apenas as evidências estruturadas fornecidas. "
                        "Não reescreva a resposta. Marque pass_check=false apenas "
                        "se houver risco factual, inventado ou incoerente."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "channel": incoming.channel,
                            "customer_message": incoming.text,
                            "agent_reply": result.reply_text,
                            "intent": result.intent,
                            "safety_reason": result.safety_reason,
                            "risk_signals": signals,
                            "evidence": _judge_evidence_payload(result),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            call_type="judge",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, JudgeVerdict):
            raise ValueError("judge_schema_missing")
        report.verdict = parsed
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        report.verdict = _failed_open_verdict(
            exc, fail_closed=_judge_must_fail_closed(result)
        )

    if (
        mode == "enforce"
        and report.verdict is not None
        and not report.verdict.pass_check
    ):
        result.reply_text = (
            "Prefiro confirmar esses dados com a equipe antes de te responder "
            "com segurança. Um atendente humano pode te ajudar agora."
        )
        result.handoff_required = True
        result.safety_reason = "quality_judge_failed"
        report.applied = True
    return report


def attach_judge_report(result: AgentResult, report: JudgeReport) -> AgentResult:
    result.response_metadata["quality_judge"] = report.model_dump(mode="json")
    return result
