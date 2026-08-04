from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Literal

from openai import APIError
from pydantic import BaseModel, Field

from .capability_catalog import (
    RETRYABLE_API_NAMES,
    build_capability_catalog,
    format_capability_catalog_for_prompt,
)
from .commerce_context import CommerceConversationState
from .config import get_settings
from .models import AgentResult, IncomingMessage
from .quality_judge import JudgeReport, JudgeVerdict, attach_judge_report
from .runtime_context import get_current_turn
from .tray_tools import execute_tool
from .turn_runtime import LLMCallBudgetExceeded


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class RecommendedApiCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class CritiqueVerdict(BaseModel):
    score: int = Field(default=100, ge=0, le=100)
    pass_check: bool = True
    issues: list[str] = Field(default_factory=list)
    summary: str = ""
    missing_context: list[str] = Field(default_factory=list)
    recommended_apis: list[RecommendedApiCall] = Field(default_factory=list)
    retry_instruction: str = ""
    better_reply_hint: str = ""


class CritiqueLoopReport(BaseModel):
    mode: Literal["off", "shadow", "enforce"] = "off"
    attempts: int = 0
    max_retries: int = 0
    approved: bool = True
    api_calls: list[dict[str, Any]] = Field(default_factory=list)
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    regenerated: bool = False
    applied_handoff: bool = False


def _transcript_blob(recent_turns: list[dict[str, Any]] | None, *, max_chars: int = 12000) -> str:
    parts: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "unknown")
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"{role}: {content}")
    blob = "\n".join(parts)
    if len(blob) <= max_chars:
        return blob
    return blob[-max_chars:]


def _seed_args_from_context(
    *,
    state: CommerceConversationState | None,
    result: AgentResult,
) -> dict[str, Any]:
    commerce = result.commercial_data or {}
    payment = commerce.get("payment") if isinstance(commerce.get("payment"), dict) else {}
    order_id = (
        commerce.get("order_id")
        or (state.order_id if state else None)
        or (state.order_lookup_id if state else None)
    )
    session_id = (
        commerce.get("session_id")
        or (state.cart_session_id if state else None)
        or (state.order_session_id if state else None)
    )
    customer = state.checkout_draft.customer if state else None
    product = state.active_product if state and state.active_product else None
    if isinstance(product, dict):
        product_id = product.get("product_id") or product.get("id")
        product_name = product.get("name")
    elif product is not None:
        product_id = getattr(product, "product_id", None) or getattr(product, "id", None)
        product_name = getattr(product, "name", None)
    else:
        product_id = None
        product_name = None
    return {
        "order_id": str(order_id).strip() if order_id else None,
        "session_id": str(session_id).strip() if session_id else None,
        "cart_session_id": str(session_id).strip() if session_id else None,
        "payment_url": (
            payment.get("payment_url")
            or (state.order_payment_url if state else None)
        ),
        "cpf": (customer.cpf if customer else None),
        "email": (customer.email if customer else None),
        "product_id": commerce.get("product_id") or product_id,
        "query": commerce.get("query") or product_name,
    }


def _fill_api_arguments(
    call: RecommendedApiCall,
    seeds: dict[str, Any],
) -> dict[str, Any] | None:
    name = str(call.name or "").strip()
    if name not in RETRYABLE_API_NAMES:
        return None
    args = dict(call.arguments or {})
    defaults: dict[str, dict[str, Any]] = {
        "get_order_complete": {"order_id": seeds.get("order_id")},
        "get_order": {"order_id": seeds.get("order_id")},
        "get_order_payment": {"order_id": seeds.get("order_id")},
        "get_payment_options": (
            {"order_id": seeds.get("order_id")}
            if seeds.get("order_id")
            else {"cart_session_id": seeds.get("cart_session_id")}
        ),
        "get_cart": {"session_id": seeds.get("session_id")},
        "get_cart_complete": {"session_id": seeds.get("session_id")},
        "list_orders": (
            {"session_id": seeds.get("session_id")}
            if seeds.get("session_id")
            else {"limit": 5}
        ),
        "search_customer": (
            {"cpf": seeds.get("cpf")}
            if seeds.get("cpf")
            else {"email": seeds.get("email")}
        ),
        "get_product": {"product_id": seeds.get("product_id")},
        "get_product_link": {"product_id": seeds.get("product_id")},
        "check_inventory": {"product_id": seeds.get("product_id")},
        "search_products": {"query": seeds.get("query"), "limit": 5},
    }
    for key, value in (defaults.get(name) or {}).items():
        if args.get(key) in (None, "") and value not in (None, ""):
            args[key] = value
    # Drop empty values.
    cleaned = {key: value for key, value in args.items() if value not in (None, "", [])}
    if name in {"get_order_complete", "get_order", "get_order_payment"} and not cleaned.get(
        "order_id"
    ):
        return None
    if name in {"get_cart", "get_cart_complete"} and not cleaned.get("session_id"):
        return None
    if name == "search_customer" and not (cleaned.get("cpf") or cleaned.get("email")):
        return None
    if name in {"get_product", "get_product_link", "check_inventory"} and not cleaned.get(
        "product_id"
    ):
        return None
    if name == "search_products" and not cleaned.get("query"):
        return None
    if name == "get_payment_options" and not (
        cleaned.get("order_id") or cleaned.get("cart_session_id")
    ):
        return None
    return cleaned


async def run_critique_judge(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
) -> CritiqueVerdict:
    settings = get_settings()
    catalog = build_capability_catalog()
    if not settings.openai_api_key:
        return CritiqueVerdict(
            score=50,
            pass_check=True,
            issues=["openai_unavailable"],
            summary="Critique skipped; OpenAI unavailable.",
        )
    payload = {
        "customer_message": incoming.text,
        "agent_reply": result.reply_text,
        "intent": result.intent,
        "safety_reason": result.safety_reason,
        "commercial_data": result.commercial_data or {},
        "working_memory": (
            result.response_metadata.get("working_memory")
            or {}
        ),
        "commerce_state": (
            commerce_state.interpreter_payload() if commerce_state else {}
        ),
        "conversation_transcript": _transcript_blob(recent_turns),
        "available_apis": catalog.get("apis"),
        "retryable_apis": catalog.get("retryable_apis"),
        "policy": catalog.get("policy"),
    }
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=CritiqueVerdict,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é o JUÍZ redundante do agente NewStore. "
                        "Valide se a resposta cumpre o pedido do cliente com o "
                        "histórico completo e as capacidades/APIs disponíveis. "
                        "pass_check=false se a resposta negar pedido/link/pagamento "
                        "existentes no histórico, inventar fatos, ignorar contexto, "
                        "ou deixar de consultar API necessária. "
                        "Quando reprovar, liste recommended_apis (somente retryable) "
                        "com arguments concretos e retry_instruction objetiva. "
                        "Não reescreva a resposta final aqui."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            temperature=0,
            call_type="judge",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, CritiqueVerdict):
            raise ValueError("critique_schema_missing")
        return parsed
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        return CritiqueVerdict(
            score=50,
            pass_check=True,
            issues=[f"critique_failed:{type(exc).__name__}"],
            summary="Critique failed open; keep original reply.",
        )


async def _execute_recommended_apis(
    *,
    verdict: CritiqueVerdict,
    seeds: dict[str, Any],
    execute: ToolExecutor,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gathered: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    for item in verdict.recommended_apis[:4]:
        args = _fill_api_arguments(item, seeds)
        if not args:
            calls.append(
                {
                    "name": item.name,
                    "skipped": True,
                    "reason": "missing_arguments_or_not_retryable",
                }
            )
            continue
        try:
            payload = await execute(item.name, args)
        except Exception as exc:
            payload = {"error": "critique_tool_failed", "error_type": type(exc).__name__}
        calls.append(
            {
                "name": item.name,
                "arguments_keys": sorted(args.keys()),
                "ok": "error" not in payload,
            }
        )
        gathered[item.name] = payload
        # Refresh seeds from successful order lookups.
        if item.name in {"get_order_complete", "get_order"} and "error" not in payload:
            order_id = payload.get("order_id") or payload.get("id")
            if order_id:
                seeds["order_id"] = str(order_id)
            payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
            if payment.get("payment_url"):
                seeds["payment_url"] = payment.get("payment_url")
        if item.name == "get_order_payment" and "error" not in payload:
            payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else payload
            if isinstance(payment, dict) and payment.get("payment_url"):
                seeds["payment_url"] = payment.get("payment_url")
    return calls, gathered


async def _regenerate_reply(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    verdict: CritiqueVerdict,
    api_facts: dict[str, Any],
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
) -> AgentResult | None:
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_output

        messages = [
            {
                "role": "system",
                "content": (
                    "Você é o agente de RESPOSTA da NewStore. "
                    "Regenera a resposta ao cliente usando o histórico, os fatos "
                    "já conhecidos e os novos resultados de API. "
                    "Não invente dados. Se houver payment_url nos fatos, envie o link. "
                    "Resposta curta em português do Brasil para WhatsApp.\n\n"
                    + format_capability_catalog_for_prompt()
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "customer_message": incoming.text,
                        "previous_reply": result.reply_text,
                        "judge_issues": verdict.issues,
                        "missing_context": verdict.missing_context,
                        "retry_instruction": verdict.retry_instruction,
                        "better_reply_hint": verdict.better_reply_hint,
                        "conversation_transcript": _transcript_blob(recent_turns),
                        "commerce_state": (
                            commerce_state.interpreter_payload() if commerce_state else {}
                        ),
                        "previous_commercial_data": result.commercial_data or {},
                        "api_facts": api_facts,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]
        text_result = await generate_text_output(
            model=settings.openai_model,
            messages=messages,
            temperature=0.2,
            call_type="response_composition",
        )
        content = text_result.text
        if not content or not content.strip():
            return None
        regenerated = result.model_copy(deep=True)
        regenerated.reply_text = content.strip()
        commercial = dict(regenerated.commercial_data or {})
        if api_facts:
            commercial["critique_api_facts"] = {
                key: ("error" not in value) if isinstance(value, dict) else True
                for key, value in api_facts.items()
            }
            payment_url = None
            for payload in api_facts.values():
                if not isinstance(payload, dict):
                    continue
                payment = payload.get("payment")
                if isinstance(payment, dict) and payment.get("payment_url"):
                    payment_url = payment.get("payment_url")
                elif payload.get("payment_url"):
                    payment_url = payload.get("payment_url")
            if payment_url:
                payment_block = dict(commercial.get("payment") or {})
                payment_block["payment_url"] = payment_url
                commercial["payment"] = payment_block
                if commerce_state is not None:
                    commerce_state.order_payment_url = str(payment_url)
            order_id = None
            for payload in api_facts.values():
                if isinstance(payload, dict) and (
                    payload.get("order_id") or payload.get("id")
                ):
                    order_id = payload.get("order_id") or payload.get("id")
                    break
            if order_id:
                commercial["order_id"] = str(order_id)
        regenerated.commercial_data = commercial
        regenerated.response_metadata = dict(regenerated.response_metadata or {})
        regenerated.response_metadata["critique_regenerated"] = True
        return regenerated
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError):
        return None


async def apply_response_critique_loop(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
    mode: Literal["off", "shadow", "enforce"] | None = None,
    max_retries: int | None = None,
    execute: ToolExecutor | None = None,
) -> tuple[AgentResult, CritiqueLoopReport]:
    """Dual-agent critique: judge draft, optionally call APIs and regenerate before send."""
    settings = get_settings()
    critique_mode: Literal["off", "shadow", "enforce"] = mode or getattr(
        settings,
        "agent_critique_mode",
        "off",
    )
    retries = (
        max_retries
        if max_retries is not None
        else int(getattr(settings, "agent_critique_max_retries", 2))
    )
    report = CritiqueLoopReport(mode=critique_mode, max_retries=retries)
    if critique_mode == "off":
        return result, report

    try:
        return await _run_response_critique_loop(
            incoming=incoming,
            result=result,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
            critique_mode=critique_mode,
            retries=retries,
            report=report,
            execute=execute,
        )
    except Exception as exc:
        # Critique must never take down the WhatsApp reply path.
        print("[agent.critique.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        result.response_metadata["response_critique"] = {
            **report.model_dump(mode="json"),
            "error_type": type(exc).__name__,
            "skipped": True,
        }
        return result, report


async def _run_response_critique_loop(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
    critique_mode: Literal["off", "shadow", "enforce"],
    retries: int,
    report: CritiqueLoopReport,
    execute: ToolExecutor | None,
) -> tuple[AgentResult, CritiqueLoopReport]:
    executor = execute or execute_tool
    current = result
    seeds = _seed_args_from_context(state=commerce_state, result=current)
    attempt = 0
    while True:
        attempt += 1
        report.attempts = attempt
        verdict = await run_critique_judge(
            incoming=incoming,
            result=current,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
        report.verdicts.append(verdict.model_dump(mode="json"))
        print("[agent.critique]", {
            "attempt": attempt,
            "pass_check": verdict.pass_check,
            "score": verdict.score,
            "issues": verdict.issues[:5],
            "recommended_apis": [item.name for item in verdict.recommended_apis[:4]],
        })
        if verdict.pass_check:
            report.approved = True
            break

        if critique_mode == "shadow" or attempt > retries:
            report.approved = False
            if critique_mode == "enforce" and attempt > retries:
                current.reply_text = (
                    "Prefiro confirmar esses dados com a equipe antes de te responder "
                    "com segurança. Um atendente humano pode te ajudar agora."
                )
                current.handoff_required = True
                current.safety_reason = "response_critique_failed"
                report.applied_handoff = True
            break

        api_calls, api_facts = await _execute_recommended_apis(
            verdict=verdict,
            seeds=seeds,
            execute=executor,
        )
        report.api_calls.extend(api_calls)
        regenerated = await _regenerate_reply(
            incoming=incoming,
            result=current,
            verdict=verdict,
            api_facts=api_facts,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
        if regenerated is None:
            report.approved = False
            current.reply_text = (
                "Prefiro confirmar esses dados com a equipe antes de te responder "
                "com segurança. Um atendente humano pode te ajudar agora."
            )
            current.handoff_required = True
            current.safety_reason = "response_critique_regenerate_failed"
            report.applied_handoff = True
            break
        current = regenerated
        report.regenerated = True
        seeds = _seed_args_from_context(state=commerce_state, result=current)

    # Mirror into legacy quality_judge metadata for observability continuity.
    last = report.verdicts[-1] if report.verdicts else None
    if last is not None:
        attach_judge_report(
            current,
            JudgeReport(
                triggered=True,
                mode=critique_mode,
                reason="response_critique",
                verdict=JudgeVerdict(
                    score=int(last.get("score") or 0),
                    pass_check=bool(last.get("pass_check")),
                    issues=list(last.get("issues") or []),
                    summary=str(last.get("summary") or ""),
                ),
                applied=report.applied_handoff,
            ),
        )
    current.response_metadata["response_critique"] = report.model_dump(mode="json")
    runtime = get_current_turn()
    if runtime is not None:
        runtime.judge_mode = critique_mode
        runtime.judge_triggered = True
        if report.applied_handoff:
            runtime.register_fallback("response_critique_failed")
    return current, report
