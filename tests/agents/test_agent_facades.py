import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.openai_agent as openai_agent
from app.agents import commerce, door
from app.agents.door import generate_agent_reply_async
from app.models import IncomingMessage, SalesInterpretation
from app.sales.interpreter import interpretation_to_plan as sales_plan
from app.sales_agent import (
    handle_sales_message,
    interpret_message,
    interpretation_to_plan as agent_plan,
)


def test_openai_agent_shim_is_the_door_module():
    assert openai_agent is door
    assert openai_agent.generate_agent_reply_async is generate_agent_reply_async
    assert openai_agent.build_agent_input is door.build_agent_input


def test_sync_legacy_path_lives_on_door_legacy():
    from app.agents import door_legacy

    assert door.generate_agent_reply is door_legacy.generate_agent_reply
    assert door.generate_openai_reply is door_legacy.generate_openai_reply
    assert openai_agent.generate_agent_reply is door_legacy.generate_agent_reply


def test_commerce_facade_reexports_sales_entry_points():
    assert commerce.interpret_message is interpret_message
    assert commerce.handle_sales_message is handle_sales_message
    assert commerce.handle_sales_message.__module__ == "app.agents.commerce"
    assert commerce.interpretation_to_plan is agent_plan
    assert commerce.interpretation_to_plan is sales_plan
    assert commerce.try_commerce_resume is not None
    assert commerce.try_commerce_checkout_routes is not None
    from app.sales_agent import _handle_sales_catalog_inner

    assert callable(_handle_sales_catalog_inner)


def test_commerce_resume_slice_returns_stored_payment():
    from app.agents.commerce_resume import try_commerce_resume
    from app.commerce.commerce_context import CommerceConversationState

    state = CommerceConversationState(
        order_id="99999",
        order_payment_url="https://pay.example/x",
        order_payment_status="pending",
        pending_action="awaiting_payment",
    )
    result = try_commerce_resume(
        IncomingMessage(text="manda o pix"),
        None,
        state,
    )
    assert result is not None
    assert result.response_metadata.get("response_source") == (
        "context_resume_payment_url"
    )
    assert "pay.example" in result.reply_text


def test_agents_init_does_not_eagerly_import_commerce():
    tree = ast.parse(Path("app/agents/__init__.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not any("commerce" in name for name in imported)
    assert not any("sales_agent" in name for name in imported)
    assert not any("double_check" in name for name in imported)


def test_package_getattr_commerce_matches_sales():
    from app.agents import interpret_message as packaged

    assert packaged is interpret_message


def test_double_check_facade_reexports_verify():
    from app.agents.double_check import apply_double_check as facade
    from app.verify.double_check import apply_double_check as body

    assert facade is body


@pytest.mark.asyncio
async def test_door_does_not_reset_browse_when_pipeline_already_did(monkeypatch):
    reset_calls: list[int] = []

    def fake_reset(state):
        reset_calls.append(1)
        return state

    async def interpret(*_a, **_k):
        return SalesInterpretation(
            domain="greeting",
            goal=None,
            subject={},
            preferences={},
            references_previous_context=False,
            needs_clarification=False,
            confidence=0.9,
            answer_strategy="acknowledge",
        )

    monkeypatch.setattr(
        "app.sales.dialogue_phase.reset_browse_memory_keep_orders",
        fake_reset,
    )
    monkeypatch.setattr(
        openai_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            openai_model="gpt-test",
            tray_adapter_url="",
            tray_adapter_token="",
            agent_db_persona_enabled=False,
            audio_inbound_enabled=False,
            database_url="",
            agent_history_hard_cap=20,
            agent_history_limit=8,
        ),
    )
    monkeypatch.setattr(openai_agent, "interpret_message", interpret)
    monkeypatch.setattr(
        openai_agent,
        "handle_sales_message",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no sales")),
    )
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(
        openai_agent,
        "should_request_human_handoff",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_k: [])

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="quero um relógio"),
        {"_browse_reset_this_turn": True, "_commerce_state": {}},
    )
    assert reset_calls == []
    assert result.intent in {"general", "commerce"}
