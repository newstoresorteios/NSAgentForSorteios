"""Independent DoubleCheck — phase 0 deterministic, phase 1 cheap LLM.

Rebuilds the turn contract from the customer text and listed IDs. Does not
inherit SalesInterpretation. Enforce only swaps a known payment resume
(PIX denied, greeting-in-checkout, phase-1 PIX veto). Other vetoes stay
on the original copy. Phase 1 never invents SKU or price.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.commerce.commerce_context import CommerceConversationState
from app.identity.greeting_policy import is_generic_greeting_reply
from app.models import AgentResult, IncomingMessage
from app.verify import log_swallowed

_PAYMENT_DENY_RE = re.compile(
    r"("
    r"n[aã]o (tenho|encontrei|consegui|achei|possuo).{0,48}(link|pix|pagamento)"
    r"|sem (o )?link (de )?(pagamento|pix)"
    r"|n[aã]o (tenho|possuo) (o )?(link|pix)"
    r")",
    re.IGNORECASE,
)

_PURCHASE_PENDING = frozenset(
    {
        "create_cart",
        "confirm_cart",
        "awaiting_variant",
        "awaiting_payment",
        "checkout",
        "awaiting_order_customer_document",
    }
)

_SKIP_INTENTS = frozenset({"greeting", "out_of_scope", "handoff", "raffle"})
_PHASE1_BUDGET_CEILING = 3
_PAYMENT_ASK_RE = re.compile(
    r"\b(pix|link de pagamento|link do pagamento|boleto)\b",
    re.IGNORECASE,
)
_ORDER_ASK_RE = re.compile(
    r"\b(pedido|pagamento|checkout)\b",
    re.IGNORECASE,
)
_PRICE_ASK_RE = re.compile(
    r"\b(quanto custa|pre[cç]o|valor|parcela)\b",
    re.IGNORECASE,
)
_PHASE1_SYSTEM = (
    "Você é um juiz independente do agente de vendas. "
    "Não reescreva a resposta. Não sugira APIs. "
    "action=approve se a reply responde o pedido com os fatos listados. "
    "action=veto se inventou preço, link ou SKU, ignorou PIX/pedido "
    "existente, ou não atendeu o pedido com estes IDs. "
    "action=handoff só se pagamento ou pedido foi afirmado sem evidência. "
    "code deve ser pix, price, order, sku ou unanswered."
)
_ENFORCE_PAYMENT_CODES = frozenset(
    {"pix_denied", "greeting_in_checkout", "pix"}
)
_LOW_RISK_SOURCES = frozenset(
    {
        "local_greeting",
        "context_resume_soft",
        "handoff",
        "guardrail",
        "out_of_scope",
        "local_raffle",
        "farewell",
        "persona_greeting",
    }
)


class DoubleCheckIssue(BaseModel):
    code: str
    reason: str


class DoubleCheckVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "veto", "handoff"] = "approve"
    code: str = ""
    reason: str = ""

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        from app.llm.openai_strict_schema import apply_openai_strict_schema

        schema = handler(core_schema)
        return apply_openai_strict_schema(schema)


class DoubleCheckReport(BaseModel):
    mode: Literal["off", "shadow", "enforce"] = "off"
    skipped: bool = False
    skip_reason: str | None = None
    approved: bool = True
    issues: list[DoubleCheckIssue] = Field(default_factory=list)
    applied: bool = False
    applied_code: str | None = None
    phase1_ran: bool = False
    phase1_gate: str | None = None
    phase1_signals: list[str] = Field(default_factory=list)
    phase1_verdict: dict[str, Any] | None = None


def _presented(result: AgentResult) -> list[dict[str, Any]]:
    products = (result.commercial_data or {}).get("products") or []
    return [item for item in products if isinstance(item, dict)]


def _payment_url(state: CommerceConversationState | None, result: AgentResult) -> str:
    if state is not None:
        url = str(getattr(state, "order_payment_url", None) or "").strip()
        if url:
            return url
    payment = (result.commercial_data or {}).get("payment")
    if isinstance(payment, dict):
        return str(payment.get("payment_url") or payment.get("url") or "").strip()
    meta = result.response_metadata or {}
    payment_state = meta.get("payment_state")
    if isinstance(payment_state, dict):
        return str(payment_state.get("order_payment_url") or "").strip()
    return ""


def _should_skip(
    incoming: IncomingMessage,
    result: AgentResult,
) -> str | None:
    if result.handoff_required:
        return "human_handoff"
    source = str((result.response_metadata or {}).get("response_source") or "")
    if source in _LOW_RISK_SOURCES:
        return f"deterministic:{source}"
    intent = str(result.intent or "").strip().casefold()
    domain = str((result.response_metadata or {}).get("domain") or "").strip().casefold()
    if intent in _SKIP_INTENTS or domain in {"greeting", "raffle", "guardrail"}:
        if not (result.commercial_data or {}):
            return f"non_commercial:{intent or domain or 'intent'}"
    text = (incoming.text or "").strip()
    if not text and not (result.reply_text or "").strip():
        return "empty_turn"
    return None


def _issue(code: str, reason: str) -> DoubleCheckIssue:
    return DoubleCheckIssue(code=code, reason=reason)


def run_phase0_double_check(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None = None,
) -> list[DoubleCheckIssue]:
    """Independent deterministic vetoes. Does not read SalesInterpretation."""
    issues: list[DoubleCheckIssue] = []
    reply = result.reply_text or ""
    products = _presented(result)
    payment_url = _payment_url(commerce_state, result)

    if payment_url and payment_url not in reply:
        if _PAYMENT_DENY_RE.search(reply):
            issues.append(
                _issue("pix_denied", "state_has_payment_url_reply_denies_link")
            )

    try:
        from app.sales.turn_contract import inbound_from_message

        view = inbound_from_message(incoming.text, None)
    except Exception as exc:
        log_swallowed("double_check.inbound_view", exc)
        view = None

    if view is not None and view.budget_max is not None and products:
        try:
            from app.catalog.retrieval.price import effective_price

            for item in products:
                price = effective_price(item)
                if price is not None and price > float(view.budget_max):
                    issues.append(
                        _issue("budget_over", "listed_price_above_message_ceiling")
                    )
                    break
        except Exception as exc:
            log_swallowed("double_check.budget", exc)

    if view is not None and view.color and products:
        try:
            from app.catalog.product_retrieval import product_conflicts_dial_color

            token = str(view.color).strip().casefold()
            if token and all(
                product_conflicts_dial_color(item, (token,)) for item in products
            ):
                issues.append(
                    _issue("color_mismatch", "message_color_absent_from_shortlist")
                )
        except Exception as exc:
            log_swallowed("double_check.color", exc)

    if result.safety_reason == "answer_council_blocked" and products:
        issues.append(
            _issue("council_blocked_listing", "council_blocked_but_products_remain")
        )

    pending = str(getattr(commerce_state, "pending_action", None) or "")
    checkout_open = bool(
        commerce_state is not None
        and (
            payment_url
            or pending in _PURCHASE_PENDING
            or bool(getattr(commerce_state, "order_id", None))
        )
    )
    source = str((result.response_metadata or {}).get("response_source") or "")
    if (
        checkout_open
        and is_generic_greeting_reply(reply)
        and source
        not in {
            "context_resume_payment_url",
            "context_resume",
            "farewell",
        }
    ):
        issues.append(_issue("greeting_in_checkout", "generic_greeting_during_checkout"))

    active = getattr(commerce_state, "active_product", None) if commerce_state else None
    locked_id = str(getattr(active, "product_id", None) or "").strip()
    if locked_id and products and pending in _PURCHASE_PENDING:
        listed_ids = {
            str(item.get("id") or item.get("product_id") or "").strip()
            for item in products
        }
        listed_ids.discard("")
        if listed_ids and locked_id not in listed_ids:
            issues.append(
                _issue("sku_lock_mismatch", "purchase_lock_id_missing_from_shortlist")
            )

    if view is not None and view.model and products:
        try:
            from app.catalog.product_retrieval import required_model_tokens
            from app.catalog.retrieval.text import product_text

            tokens = required_model_tokens(view.model)
            if tokens and not any(
                all(token in product_text(item) for token in tokens)
                for item in products
            ):
                issues.append(
                    _issue("model_mismatch", "message_model_absent_from_shortlist")
                )
        except Exception as exc:
            log_swallowed("double_check.model", exc)

    return issues


def _payment_resume_result(
    commerce_state: CommerceConversationState | None,
) -> AgentResult | None:
    if commerce_state is None:
        return None
    try:
        from app.memory.context_resume import build_pending_payment_resume_result

        return build_pending_payment_resume_result(commerce_state)
    except Exception as exc:
        log_swallowed("double_check.pix_resume", exc)
        return None


def _apply_payment_resume(
    *,
    report: DoubleCheckReport,
    commerce_state: CommerceConversationState | None,
    applied_code: str,
) -> AgentResult | None:
    if report.mode != "enforce" or applied_code not in _ENFORCE_PAYMENT_CODES:
        return None
    resume = _payment_resume_result(commerce_state)
    if resume is None:
        return None
    report.applied = True
    report.applied_code = applied_code
    resume.response_metadata = dict(resume.response_metadata or {})
    resume.response_metadata["double_check"] = report.model_dump(mode="json")
    return resume


def apply_double_check(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None = None,
    mode: str = "enforce",
) -> tuple[AgentResult, DoubleCheckReport]:
    normalized = str(mode or "enforce").strip().casefold()
    if normalized not in {"off", "shadow", "enforce"}:
        normalized = "enforce"
    report = DoubleCheckReport(mode=normalized)
    if normalized == "off":
        report.skipped = True
        report.skip_reason = "configured_off"
        return result, report
    skip = _should_skip(incoming, result)
    if skip:
        report.skipped = True
        report.skip_reason = skip
        result.response_metadata = dict(result.response_metadata or {})
        result.response_metadata["double_check"] = report.model_dump(mode="json")
        return result, report

    report.issues = run_phase0_double_check(
        incoming=incoming,
        result=result,
        commerce_state=commerce_state,
    )
    report.approved = not report.issues
    result.response_metadata = dict(result.response_metadata or {})

    if normalized == "enforce" and not report.approved:
        for issue in report.issues:
            resume = _apply_payment_resume(
                report=report,
                commerce_state=commerce_state,
                applied_code=issue.code,
            )
            if resume is not None:
                print(
                    "[verify.double_check]",
                    {
                        "mode": normalized,
                        "approved": False,
                        "applied": True,
                        "codes": [issue.code for issue in report.issues],
                    },
                )
                return resume, report

    result.response_metadata["double_check"] = report.model_dump(mode="json")
    if not report.approved:
        print(
            "[verify.double_check]",
            {
                "mode": normalized,
                "approved": False,
                "applied": False,
                "codes": [issue.code for issue in report.issues],
            },
        )
    return result, report


def collect_phase1_risk_signals(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None = None,
) -> list[str]:
    """High-stakes gates only — a priced listing alone does not spend LLM."""
    signals: list[str] = []
    text = incoming.text or ""
    payment_url = _payment_url(commerce_state, result)
    pending = str(getattr(commerce_state, "pending_action", None) or "")
    order_id = str(getattr(commerce_state, "order_id", None) or "").strip()
    active = getattr(commerce_state, "active_product", None) if commerce_state else None
    if payment_url:
        signals.append("payment_url_present")
    if _PAYMENT_ASK_RE.search(text):
        signals.append("inbound_asks_payment")
    if _ORDER_ASK_RE.search(text):
        signals.append("inbound_asks_order")
    if _PRICE_ASK_RE.search(text):
        signals.append("inbound_asks_price")
    if order_id or pending in _PURCHASE_PENDING:
        signals.append("order_or_checkout")
    if active is not None and pending in _PURCHASE_PENDING:
        signals.append("sku_lock")
    try:
        from app.sales.discovery import message_states_budget

        if message_states_budget(text):
            signals.append("inbound_budget")
    except Exception as exc:
        log_swallowed("double_check.budget_signal", exc)
    return list(dict.fromkeys(signals))


def should_run_phase1_double_check(
    *,
    report: DoubleCheckReport,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None = None,
    critique_regenerated: bool = False,
    openai_call_count: int = 0,
    openai_api_key: str | None = None,
) -> tuple[bool, str, list[str]]:
    if report.skipped:
        return False, report.skip_reason or "already_skipped", []
    if report.applied:
        return False, "phase0_applied", []
    if report.issues:
        return False, "phase0_already_vetoed", []
    if critique_regenerated:
        return False, "skipped_after_critique_regenerate", []
    if int(openai_call_count or 0) >= _PHASE1_BUDGET_CEILING:
        return False, "llm_budget", []
    signals = collect_phase1_risk_signals(
        incoming=incoming,
        result=result,
        commerce_state=commerce_state,
    )
    if not signals:
        return False, "no_high_risk_signal", []
    if not (openai_api_key or "").strip():
        return False, "openai_unavailable", signals
    return True, f"risk:{signals[0]}", signals


def _phase1_packet(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None,
    signals: list[str],
) -> dict[str, Any]:
    products = []
    try:
        from app.catalog.retrieval.price import effective_price
    except Exception:
        effective_price = None  # type: ignore[assignment]
    for item in _presented(result)[:5]:
        price = None
        if effective_price is not None:
            try:
                price = effective_price(item)
            except Exception as exc:
                log_swallowed("double_check.phase1_price", exc)
        products.append(
            {
                "id": str(item.get("id") or item.get("product_id") or ""),
                "name": str(item.get("name") or "")[:80],
                "price": price,
            }
        )
    active = getattr(commerce_state, "active_product", None) if commerce_state else None
    inbound_budget = None
    inbound_color = None
    try:
        from app.sales.turn_contract import inbound_from_message

        view = inbound_from_message(incoming.text, None)
        inbound_budget = view.budget_max
        inbound_color = view.color
    except Exception as exc:
        log_swallowed("double_check.phase1_view", exc)
    return {
        "customer_message": incoming.text,
        "agent_reply": result.reply_text,
        "signals": signals,
        "payment_url": _payment_url(commerce_state, result) or None,
        "order_id": str(getattr(commerce_state, "order_id", None) or "") or None,
        "pending_action": str(getattr(commerce_state, "pending_action", None) or "")
        or None,
        "active_product_id": str(getattr(active, "product_id", None) or "") or None,
        "products": products,
        "inbound_budget_max": inbound_budget,
        "inbound_color": inbound_color,
    }


async def run_phase1_double_check(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None,
    signals: list[str],
) -> DoubleCheckVerdict:
    import json

    from app.llm.openai_gateway import parse_structured_output
    from app.llm.openai_models import resolve_openai_model

    parse_result = await parse_structured_output(
        model=resolve_openai_model("fast"),
        text_format=DoubleCheckVerdict,
        messages=[
            {"role": "system", "content": _PHASE1_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    _phase1_packet(
                        incoming=incoming,
                        result=result,
                        commerce_state=commerce_state,
                        signals=signals,
                    ),
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0,
        call_type="double_check",
    )
    parsed = parse_result.parsed
    if not isinstance(parsed, DoubleCheckVerdict):
        raise ValueError("double_check_schema_missing")
    return parsed


async def apply_double_check_async(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    commerce_state: CommerceConversationState | None = None,
    mode: str = "enforce",
    critique_regenerated: bool = False,
    openai_call_count: int = 0,
) -> tuple[AgentResult, DoubleCheckReport]:
    result, report = apply_double_check(
        incoming=incoming,
        result=result,
        commerce_state=commerce_state,
        mode=mode,
    )
    if report.mode == "off":
        return result, report
    from app.config import get_settings

    settings = get_settings()
    run, gate, signals = should_run_phase1_double_check(
        report=report,
        incoming=incoming,
        result=result,
        commerce_state=commerce_state,
        critique_regenerated=critique_regenerated,
        openai_call_count=openai_call_count,
        openai_api_key=getattr(settings, "openai_api_key", ""),
    )
    report.phase1_gate = gate
    report.phase1_signals = signals
    if not run:
        result.response_metadata = dict(result.response_metadata or {})
        result.response_metadata["double_check"] = report.model_dump(mode="json")
        return result, report

    try:
        from openai import APIError

        from app.llm.openai_errors import OpenAIGatewayError
        from app.ops.turn_runtime import LLMCallBudgetExceeded

        verdict = await run_phase1_double_check(
            incoming=incoming,
            result=result,
            commerce_state=commerce_state,
            signals=signals,
        )
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        log_swallowed("double_check.phase1", exc)
        report.phase1_gate = f"phase1_failed:{type(exc).__name__}"
        result.response_metadata = dict(result.response_metadata or {})
        result.response_metadata["double_check"] = report.model_dump(mode="json")
        return result, report

    report.phase1_ran = True
    report.phase1_verdict = verdict.model_dump(mode="json")
    if verdict.action != "approve":
        report.approved = False
        report.issues.append(
            _issue(
                verdict.code.strip() or f"llm_{verdict.action}",
                verdict.reason or f"phase1_{verdict.action}",
            )
        )
        print(
            "[verify.double_check]",
            {
                "mode": report.mode,
                "phase": 1,
                "approved": False,
                "action": verdict.action,
                "code": verdict.code,
            },
        )
        if report.mode == "enforce" and verdict.action == "veto":
            resume = _apply_payment_resume(
                report=report,
                commerce_state=commerce_state,
                applied_code=verdict.code.strip().casefold() or "pix",
            )
            if resume is not None:
                return resume, report

    result.response_metadata = dict(result.response_metadata or {})
    result.response_metadata["double_check"] = report.model_dump(mode="json")
    return result, report
