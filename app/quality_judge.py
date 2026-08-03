from __future__ import annotations

import json
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from .config import get_settings
from .models import AgentResult, IncomingMessage
from .openai_runtime import execute_openai_call
from .runtime_context import get_current_turn
from .turn_runtime import LLMCallBudgetExceeded


class JudgeVerdict(BaseModel):
    score: int = Field(default=100, ge=0, le=100)
    pass_check: bool = True
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


class JudgeReport(BaseModel):
    triggered: bool = False
    mode: Literal["off", "shadow", "enforce"] = "off"
    reason: str | None = None
    verdict: JudgeVerdict | None = None
    applied: bool = False


def should_trigger_judge(
    *,
    risk_score: int,
    factual_valid: bool,
    handoff_required: bool,
    openai_call_count: int,
    threshold: int = 70,
) -> tuple[bool, str | None]:
    if handoff_required:
        return True, "handoff_required"
    if not factual_valid:
        return True, "factual_validation_failed"
    if risk_score >= threshold:
        return True, "high_risk"
    if openai_call_count >= 3:
        return True, "complex_llm_turn"
    return False, None


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
    triggered, reason = should_trigger_judge(
        risk_score=risk_score,
        factual_valid=factual_valid,
        handoff_required=result.handoff_required,
        openai_call_count=openai_call_count,
        threshold=getattr(
            settings,
            "agent_quality_judge_risk_threshold",
            70,
        ),
    )
    report.triggered = triggered
    report.reason = reason
    runtime = get_current_turn()
    if runtime is not None:
        runtime.judge_mode = mode
        runtime.judge_triggered = triggered
    if not triggered:
        return report

    if not settings.openai_api_key:
        report.verdict = JudgeVerdict(
            score=50,
            pass_check=True,
            issues=["openai_unavailable"],
            summary="Judge skipped because OpenAI is unavailable.",
        )
        return report

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await execute_openai_call(
            call_type="judge",
            operation=lambda: client.chat.completions.parse(
                model=settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Avalie a qualidade e segurança da resposta do agente. "
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
                                "commercial_data_keys": sorted(
                                    (result.commercial_data or {}).keys()
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0,
                response_format=JudgeVerdict,
            ),
        )
        parsed = response.choices[0].message.parsed if response.choices else None
        if not isinstance(parsed, JudgeVerdict):
            raise ValueError("judge_schema_missing")
        report.verdict = parsed
    except (
        APIError,
        LLMCallBudgetExceeded,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        report.verdict = JudgeVerdict(
            score=50,
            pass_check=True,
            issues=[f"judge_failed:{type(exc).__name__}"],
            summary="Judge failed open; shadow/off keeps original reply.",
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
