from types import SimpleNamespace

import pytest

from app.agents.door_gates import try_farewell
from app.agents.door_order import try_order_resume_route
from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage


def test_try_farewell_skips_open_checkout():
    state = CommerceConversationState(
        pending_action="awaiting_payment",
        order_id="25422",
        order_payment_url="https://pay.example/25422",
        dialogue_phase="checkout",
    )
    assert try_farewell(IncomingMessage(text="tchau"), {}, state) is None


def test_try_farewell_skips_live_shortlist():
    state = CommerceConversationState(
        dialogue_phase="shortlist",
        last_presented_products=[
            {"position": 1, "product_id": "1", "name": "Seiko 5"},
        ],
    )
    assert try_farewell(IncomingMessage(text="obrigado"), {}, state) is None


def test_try_farewell_allows_paid_url_leftover():
    state = CommerceConversationState(
        order_id="25422",
        order_payment_url="https://pay.example/25422",
        order_payment_status="confirmed",
    )
    result = try_farewell(IncomingMessage(text="tchau"), {}, state)
    assert result is not None
    assert result.response_metadata.get("response_source") == "farewell"


def test_payment_method_question_is_not_unpaid_resume():
    from app.memory.context_resume import is_unpaid_order_resume_request

    assert is_unpaid_order_resume_request("quais formas de pagamento?") is False
    assert is_unpaid_order_resume_request("não paguei") is True


def test_try_farewell_still_closes_idle_session():
    result = try_farewell(
        IncomingMessage(text="tchau"),
        {},
        CommerceConversationState(),
    )
    assert result is not None
    assert result.response_metadata.get("response_source") == "farewell"


@pytest.mark.asyncio
async def test_live_inspect_without_url_does_not_recite_stale(monkeypatch):
    import app.openai_agent as openai_agent

    stale = "https://pay.example/stale"

    async def fake_inspect(*, state, execute, order_id=None):
        return AgentResult(
            reply_text=(
                f"Seu pedido {order_id} ainda está aguardando pagamento, "
                "mas não encontrei o link agora."
            ),
            intent="commerce",
            commercial_data={
                "order_id": order_id,
                "payment": {"status": "pending"},
            },
            response_metadata={"used_tray": True, "domain": "commerce"},
        )

    monkeypatch.setattr(openai_agent, "inspect_order_payment", fake_inspect)

    result = await try_order_resume_route(
        message=IncomingMessage(text="sim"),
        commerce_state=CommerceConversationState(
            order_id="25422",
            order_payment_url=stale,
            pending_action="awaiting_payment",
            order_payment_status="pending",
        ),
        context_handles={},
        fresh_start=False,
        soft_greeting=False,
        resume_pending_order_early=False,
        order_reference=None,
    )
    assert result is not None
    assert stale not in result.reply_text
    assert "não encontrei o link" in result.reply_text.casefold()


@pytest.mark.asyncio
async def test_sim_with_numeric_order_inspects_instead_of_stale_url(monkeypatch):
    import app.openai_agent as openai_agent

    stale = "https://pay.example/stale"
    calls = {"inspect": 0}

    async def fake_inspect(*, state, execute, order_id=None):
        calls["inspect"] += 1
        return AgentResult(
            reply_text=(
                f"Seu pedido {order_id} ainda está aguardando pagamento, "
                "mas não encontrei o link agora."
            ),
            intent="commerce",
            commercial_data={"order_id": order_id, "payment": {"status": "pending"}},
            response_metadata={"used_tray": True, "domain": "commerce"},
        )

    async def no_interpret(*_a, **_k):
        raise AssertionError("sim on unpaid order must inspect, not interpret")

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_k: [])
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(
        openai_agent,
        "should_request_human_handoff",
        lambda _m, **_k: None,
    )
    monkeypatch.setattr(openai_agent, "inspect_order_payment", fake_inspect)
    monkeypatch.setattr(openai_agent, "interpret_message", no_interpret)
    monkeypatch.setattr(openai_agent, "handle_sales_message", no_interpret)

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="sim", conversation_id="conv-pix", sender_phone="5511999999999"),
        {
            "_commerce_state": {
                "order_id": "25422",
                "order_payment_url": stale,
                "pending_action": "awaiting_payment",
                "order_payment_status": "pending",
                "dialogue_phase": "checkout",
            }
        },
    )
    assert calls["inspect"] == 1
    assert stale not in (result.reply_text or "")
    assert "não encontrei o link" in (result.reply_text or "").casefold()


@pytest.mark.asyncio
async def test_generate_openai_reply_async_never_opens_tool_loop(monkeypatch):
    import app.llm.openai_gateway as gateway
    import app.openai_agent as openai_agent

    called = {"text": 0, "tools": 0}

    async def fake_text(**_kwargs):
        called["text"] += 1
        return SimpleNamespace(text="Abrimos de segunda a sábado.")

    async def boom_tools(**_kwargs):
        called["tools"] += 1
        raise AssertionError("legacy door must not open TOOL_SCHEMAS")

    monkeypatch.setattr(
        openai_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="sk-test",
            openai_model="gpt-test",
            tray_adapter_url="https://tray.example",
            tray_adapter_token="tok",
            max_reply_chars=900,
        ),
    )
    monkeypatch.setattr(gateway, "generate_text_output", fake_text)
    monkeypatch.setattr(gateway, "run_tool_loop_output", boom_tools)

    result = await openai_agent.generate_openai_reply_async(
        IncomingMessage(text="quanto custa o seiko?"),
        {},
        {"primary_intent": "commerce", "scope_domain": "store_general"},
    )
    assert called["text"] == 1
    assert called["tools"] == 0
    assert "sábado" in result.reply_text
