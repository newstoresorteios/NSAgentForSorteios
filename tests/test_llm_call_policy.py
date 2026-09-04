from app.config import get_settings
from app.llm.llm_call_policy import (
    resolve_turn_critique_mode,
    should_run_llm_critique,
    should_run_quality_judge,
)
from app.models import AgentResult, IncomingMessage
from app.verify.quality_judge import is_low_risk_judge_skip


def test_should_run_llm_critique_skips_low_risk(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    monkeypatch.setenv("AGENT_CRITIQUE_SHADOW_SAMPLE_RATE", "0")
    get_settings.cache_clear()
    run, reason, signals = should_run_llm_critique(
        incoming=IncomingMessage(channel="whatsapp", text="tem relógio?"),
        result=AgentResult(
            reply_text="Tenho opções",
            intent="commerce",
            commercial_data={"products": [{"id": "1"}]},
        ),
        critique_mode="shadow",
        risk_score=10,
        factual_valid=True,
    )
    assert run is False
    assert reason == "risk_gate_skip"
    assert "factual_validation_failed" not in signals


def test_should_run_llm_critique_on_factual_fail(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    get_settings.cache_clear()
    result = AgentResult(
        reply_text="Esse relógio custa R$ 10",
        intent="commerce",
        response_metadata={"factual_validation": {"valid": False}},
    )
    run, reason, signals = should_run_llm_critique(
        incoming=IncomingMessage(channel="whatsapp", text="quanto custa?"),
        result=result,
        critique_mode="shadow",
        risk_score=10,
        factual_valid=False,
    )
    assert run is True
    assert reason.startswith("risk:")
    assert "factual_validation_failed" in signals


def test_should_run_llm_critique_on_commerce_enforce(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    monkeypatch.setenv("AGENT_CRITIQUE_SHADOW_SAMPLE_RATE", "0")
    get_settings.cache_clear()
    run, reason, signals = should_run_llm_critique(
        incoming=IncomingMessage(channel="whatsapp", text="quanto custa?"),
        result=AgentResult(
            reply_text="Esse Seiko está R$ 3.200",
            intent="commerce",
            commercial_data={
                "products": [{"id": "1", "name": "Seiko 5", "reference": "SRPD55"}],
            },
        ),
        critique_mode="enforce",
        risk_score=10,
        factual_valid=True,
    )
    assert run is True
    assert reason.startswith("commerce_enforce:")
    assert "reply_contains_price" in signals or "commerce_products_presented" in signals


def test_resolve_turn_critique_mode_promotes_shadow_for_price(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_ENFORCE_ON_COMMERCE", "true")
    get_settings.cache_clear()
    mode, reason = resolve_turn_critique_mode(
        incoming=IncomingMessage(channel="whatsapp", text="qual o valor?"),
        result=AgentResult(
            reply_text="O valor atual é R$ 4.500",
            intent="commerce",
            commercial_data={"products": [{"id": "9", "sku": "ABC"}]},
        ),
        configured_mode="shadow",
    )
    assert mode == "enforce"
    assert reason.startswith("commerce_promote:")


def test_resolve_turn_critique_mode_keeps_shadow_without_commerce_stakes(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_ENFORCE_ON_COMMERCE", "true")
    get_settings.cache_clear()
    mode, reason = resolve_turn_critique_mode(
        incoming=IncomingMessage(channel="whatsapp", text="vocês abrem sábado?"),
        result=AgentResult(
            reply_text="Abrimos de segunda a sábado.",
            intent="store_general",
        ),
        configured_mode="shadow",
    )
    assert mode == "shadow"
    assert reason == "configured_shadow"


def test_greeting_intent_skips_critique():
    skip, reason = is_low_risk_judge_skip(
        IncomingMessage(channel="whatsapp", text="quero ver relógios"),
        AgentResult(reply_text="Olá! Como posso ajudar?", intent="greeting"),
    )
    assert skip is True
    assert reason == "greeting_intent"


def test_quality_judge_off_by_default():
    run, reason, _ = should_run_quality_judge(
        incoming=IncomingMessage(channel="whatsapp", text="x"),
        result=AgentResult(reply_text="y", intent="commerce"),
        judge_mode="off",
        factual_valid=False,
        risk_score=99,
    )
    assert run is False
    assert reason == "judge_mode_off"
