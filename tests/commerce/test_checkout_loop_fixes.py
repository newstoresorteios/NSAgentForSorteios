"""Regression tests for checkout loop / stale shortlist / OpenAI 429 fallbacks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.commerce.commerce_context import CommerceConversationState, evolve_commerce_state
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.dialogue_phase import message_resets_dialogue_to_discovery
from app.sales.intent_router import route_sales_intent


def _presented():
    return [
        {
            "position": 1,
            "product_id": "aq1",
            "name": "Baltic Aquascaphe",
            "brand": "Baltic",
        },
        {
            "position": 2,
            "product_id": "sb01",
            "name": "Baltic Classic SB01",
            "brand": "Baltic",
        },
    ]


def _checkout_state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "cart_session_id": "SESSION-5585999498149",
        "cart_url": "https://loja.example/checkout/SESSION-5585999498149",
        "pending_action": "choose_checkout_channel",
        "purchase_stage": "cart_created",
        "last_presented_products": _presented(),
        "dialogue_phase": "checkout",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interp(**kwargs) -> SalesInterpretation:
    base = {
        "domain": "commerce",
        "goal": "discover",
        "subject": {},
        "preferences": {},
        "references_previous_context": False,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "needs_clarification": True,
        "confidence": 0.6,
    }
    base.update(kwargs)
    return SalesInterpretation(**base)


@pytest.mark.offline_eval
def test_purchase_close_hold_skipped_in_checkout_with_stale_shortlist():
    route = route_sales_intent(
        interpretation=_interp(),
        plan={"intent": "clarification", "goal": "discover", "query": ""},
        message_text="tudo bem?",
        commerce_state=_checkout_state(),
        recent_turns=[],
    )
    assert route.purchase_close is False
    assert route.purchase_close_hold is False


@pytest.mark.asyncio
async def test_openai_429_greeting_not_shortlist_prompt(monkeypatch):
    import app.sales_agent as sales_agent

    interpreted = await sales_agent.interpret_message(
        IncomingMessage(text="tudo bem?"),
        commerce_state=_checkout_state(),
    )
    assert interpreted.domain == "greeting"
    assert interpreted._fallback_reason == "greeting_fast_path"

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="gpt-4.1-mini"),
    )
    monkeypatch.setattr(sales_agent, "_sales_response_with_openai", lambda *_a, **_k: None)

    async def fake_clarification(**_kwargs):
        return AgentResult(
            reply_text="Me conta o que você procura.",
            intent="commerce",
            handoff_required=False,
            response_metadata={"domain": "commerce"},
        )

    monkeypatch.setattr(sales_agent, "generate_clarification_reply", fake_clarification)

    fallback_interp = _interp()
    fallback_interp._source = "deterministic_fallback"
    fallback_interp._fallback_reason = "openai_request_failed"

    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="tudo bem?"),
        {},
        {},
        fallback_interp,
        commerce_state=_checkout_state(),
    )

    assert result is not None
    assert "Qual opção da lista" not in (result.reply_text or "")

    defensive = sales_agent._purchase_close_hold_reply(
        message=IncomingMessage(text="tudo bem?"),
        state=_checkout_state(),
        interpretation=fallback_interp,
    )
    assert "Qual opção da lista" not in defensive
    assert "WhatsApp" in defensive or "Como posso" in defensive or "ajudar" in defensive.lower()


@pytest.mark.asyncio
async def test_clarification_openai_fail_never_empty_reply(monkeypatch):
    import app.sales_agent as sales_agent
    from app.llm.openai_errors import OpenAIRateLimitGatewayError

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="gpt-4.1-mini"),
    )

    async def boom(**_kwargs):
        raise OpenAIRateLimitGatewayError("no credits")

    monkeypatch.setattr("app.llm.openai_gateway.generate_text_output", boom)

    interp = _interp()
    interp._source = "deterministic_fallback"
    interp.clarification_question = None

    result = await sales_agent.generate_clarification_reply(
        message=IncomingMessage(text="Trabalho"),
        interpretation=interp,
        discovery_state={"persona_qualification_required": False},
    )

    assert (result.reply_text or "").strip()
    assert result.reply_text != "Resposta em áudio"
    assert "marca" in result.reply_text.casefold() or "investimento" in result.reply_text.casefold()


@pytest.mark.offline_eval
def test_nenhuma_clears_shortlist_or_resets_discovery():
    assert message_resets_dialogue_to_discovery("nenhuma", None) is True

    route = route_sales_intent(
        interpretation=_interp(),
        plan={"intent": "clarification", "goal": "discover", "query": "alguma coisa"},
        message_text="nenhuma",
        commerce_state=CommerceConversationState(
            last_presented_products=_presented(),
            dialogue_phase="shortlist",
        ),
        recent_turns=[],
    )
    assert route.browse_reset is True
    assert route.purchase_close_hold is False

    previous = CommerceConversationState(
        last_presented_products=_presented(),
        active_product={"product_id": "aq1", "name": "Baltic Aquascaphe"},
        dialogue_phase="shortlist",
    )
    result = AgentResult(
        reply_text="Certo, vamos buscar outras opções.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "dialogue_phase": "discovery",
            "dialogue_phase_reset": True,
        },
    )
    updated = evolve_commerce_state(previous, result)
    assert updated.last_presented_products == []
    assert updated.active_product is None
    assert updated.dialogue_phase == "discovery"


def test_advance_whatsapp_checkout_does_not_double_evolve():
    from pathlib import Path

    source = Path("app/sales/checkout_flow.py").read_text(encoding="utf-8")
    assert (
        "current = evolve_commerce_state(state, result)\n"
        "        current = evolve_commerce_state(state, result)"
    ) not in source


def test_commerce_exception_handlers_are_not_bare_pass():
    import ast
    from pathlib import Path

    silent: list[str] = []
    for path in Path("app/commerce").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = ast.unparse(node.type) if node.type is not None else "BaseException"
            if caught not in {"Exception", "BaseException"}:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                silent.append(f"{path.as_posix()}:{node.lineno}")
    assert silent == []
