from types import SimpleNamespace

import pytest

from app.message_pipeline import commercial_products_present
from app.models import AgentResult, IncomingMessage


def test_commercial_products_present():
    empty = AgentResult(reply_text="oi", intent="general")
    assert commercial_products_present(empty) is False
    listed = AgentResult(
        reply_text="Seiko 5",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "name": "Seiko 5"}]},
    )
    assert commercial_products_present(listed) is True


def _pipeline_settings():
    return SimpleNamespace(
        audio_inbound_enabled=False,
        audio_outbound_enabled=False,
        agent_policy_mode="shadow",
        agent_factual_validation_mode="enforce",
        agent_trusted_fact_domains="",
        agent_critique_mode="off",
        agent_quality_judge_mode="off",
        agent_emergency_rollback=False,
        max_reply_chars=900,
        agent_persona_tenant_id="newstore",
    )


@pytest.mark.asyncio
async def test_listed_products_run_factual_once_after_scope_council(monkeypatch):
    import app.message_pipeline as pipeline

    order: list[str] = []

    async def fake_generate(*_a, **_k):
        order.append("generate")
        return AgentResult(
            reply_text="Seiko 5 por R$ 2.500",
            intent="commerce",
            commercial_data={
                "products": [
                    {"id": "1", "name": "Seiko 5", "current_price": 2500},
                ]
            },
            response_metadata={},
        )

    def fake_factual(result, **_k):
        order.append("factual")
        return result

    async def fake_scope(result, **_kwargs):
        order.append("scope")
        return result, None, None

    async def fake_council(result, **_kwargs):
        order.append("council")
        return result, None, None

    def fake_compliance(*, incoming, result, interpretation):
        order.append("compliance")
        return result, None

    monkeypatch.setattr(pipeline, "get_settings", _pipeline_settings)
    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", lambda **_k: {})
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_k: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "generate_agent_reply_async", fake_generate)
    monkeypatch.setattr(pipeline, "apply_factual_validation", fake_factual)
    monkeypatch.setattr(pipeline, "compose_outbound_reply", lambda incoming, result, **_k: result)
    monkeypatch.setattr(
        "app.sales.scope_send_gate.apply_scope_send_gate_with_retry",
        fake_scope,
    )
    monkeypatch.setattr(
        "app.sales.answer_council.apply_answer_council_with_retry",
        fake_council,
    )
    monkeypatch.setattr(
        "app.verify.outbound_compliance.apply_outbound_compliance",
        fake_compliance,
    )

    result = await pipeline.process_incoming_message(
        IncomingMessage(text="tem seiko?", conversation_id="gate-order"),
        {},
    )
    assert result.intent == "commerce"
    assert order.count("factual") == 1
    factual_at = order.index("factual")
    assert order.index("scope") < order.index("council") < order.index("compliance") < factual_at


@pytest.mark.asyncio
async def test_greeting_door_does_not_call_sales_handle(monkeypatch):
    from app import openai_agent
    from app.models import SalesInterpretation

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

    async def sales_must_not_run(*_a, **_k):
        raise AssertionError("greeting must not call handle_sales_message")

    async def tools_must_not_run(*_a, **_k):
        raise AssertionError("greeting must not open the tool loop")

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
    monkeypatch.setattr(openai_agent, "handle_sales_message", sales_must_not_run)
    monkeypatch.setattr(openai_agent, "generate_openai_reply_async", tools_must_not_run)
    monkeypatch.setattr(openai_agent, "detect_blocked_request", lambda _t: None)
    monkeypatch.setattr(openai_agent, "should_request_human_handoff", lambda *_a, **_k: None)
    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **_k: [])

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(text="oi"),
        {},
    )
    assert result.intent == "general"
    assert result.handoff_required is False
    assert result.reply_text
