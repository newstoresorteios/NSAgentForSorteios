import pytest

from app.models import AgentResult, IncomingMessage
from app.llm.llm_call_policy import should_run_quality_judge
from app.verify.quality_judge import (
    JudgeVerdict,
    attach_judge_report,
    collect_judge_risk_signals,
    is_low_risk_judge_skip,
    run_quality_judge,
    should_trigger_judge,
    _judge_history_payload,
)
from app.ops.runtime_context import reset_current_turn, set_current_turn
from app.ops.turn_runtime import TurnRuntimeContext
from tests.llm.openai_test_utils import install_fake_openai_client


def test_judge_triggers_on_high_risk_only():
    assert should_trigger_judge(
        risk_score=40,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
    ) == (False, None)
    assert should_trigger_judge(
        risk_score=80,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
    )[0] is True


def test_low_risk_paths_skip_judge():
    greeting = AgentResult(
        reply_text="Olá!",
        intent="general",
        response_metadata={"response_source": "local_greeting"},
    )
    assert is_low_risk_judge_skip(
        IncomingMessage(text="oi"),
        greeting,
    )[0] is True

    thanks = AgentResult(
        reply_text="Por nada!",
        intent="general",
        response_metadata={"response_source": "deterministic_fallback"},
    )
    assert is_low_risk_judge_skip(
        IncomingMessage(text="obrigado"),
        thanks,
    )[0] is True

    handoff = AgentResult(
        reply_text="Vou transferir",
        intent="handoff",
        handoff_required=True,
        response_metadata={"response_source": "handoff"},
    )
    assert should_trigger_judge(
        risk_score=90,
        factual_valid=True,
        handoff_required=True,
        openai_call_count=0,
        incoming=IncomingMessage(text="quero atendente"),
        result=handoff,
    )[0] is False


def test_payment_resume_with_url_does_not_skip_judge():
    skip, _reason = is_low_risk_judge_skip(
        IncomingMessage(text="manda o pix"),
        AgentResult(
            reply_text="Segue o link: https://pay.example/1",
            intent="commerce",
            commercial_data={
                "payment": {"payment_url": "https://pay.example/1"},
            },
            response_metadata={"response_source": "context_resume_payment_url"},
        ),
    )
    assert skip is False


def test_presented_catalog_resume_does_not_skip_judge():
    skip, _reason = is_low_risk_judge_skip(
        IncomingMessage(text="qual relógio?"),
        AgentResult(
            reply_text="1. Seiko 5",
            intent="commerce",
            commercial_data={"products": [{"id": "1", "name": "Seiko 5"}]},
            response_metadata={"response_source": "context_resume_presented_catalog"},
        ),
    )
    assert skip is False


def test_commercial_signals_trigger_judge():
    priced = AgentResult(
        reply_text="O modelo custa R$ 199,90.",
        intent="commerce",
        commercial_data={"products": [{"id": "1", "current_price": "199.90"}]},
        response_metadata={"response_source": "openai", "used_tray": True},
    )
    signals = collect_judge_risk_signals(
        result=priced,
        risk_score=20,
        factual_valid=True,
        openai_call_count=1,
    )
    assert "reply_contains_price" in signals
    assert should_trigger_judge(
        risk_score=20,
        factual_valid=True,
        handoff_required=False,
        openai_call_count=1,
        incoming=IncomingMessage(text="quanto custa?"),
        result=priced,
    )[0] is True


def test_contextual_clarification_bypasses_deterministic_skip_and_triggers_review():
    incoming = IncomingMessage(channel="whatsapp", text="azul")
    result = AgentResult(
        reply_text="Qual marca você procura?",
        intent="commerce",
        safety_reason="commerce_clarification",
        response_metadata={"response_source": "deterministic_clarification"},
    )
    history = [
        {"role": "user", "content": "Quero um relógio azul."},
        {"role": "assistant", "content": "Qual cor você prefere?"},
    ]

    assert is_low_risk_judge_skip(incoming, result)[0] is True
    assert is_low_risk_judge_skip(incoming, result, history)[0] is False
    should_run, reason, signals = should_run_quality_judge(
        incoming=incoming,
        result=result,
        judge_mode="shadow",
        recent_turns=history,
    )
    assert should_run is True
    assert reason == "risk:contextual_clarification_requires_review"
    assert "contextual_clarification_requires_review" in signals


def test_repeated_question_and_customer_repair_trigger_quality_review():
    history = [
        {"role": "assistant", "content": "Qual faixa de preço você procura?"},
    ]
    repeated = AgentResult(
        reply_text="Qual faixa de preço você procura?",
        intent="commerce",
        response_metadata={"response_source": "deterministic_clarification"},
    )
    should_run, _reason, signals = should_run_quality_judge(
        incoming=IncomingMessage(channel="whatsapp", text="até 500 reais"),
        result=repeated,
        judge_mode="shadow",
        recent_turns=history,
    )
    assert should_run is True
    assert "repeated_clarification_question" in signals

    repair_result = AgentResult(
        reply_text="Vou verificar a informação.",
        intent="commerce",
        response_metadata={"response_source": "deterministic_fallback"},
    )
    should_run, _reason, signals = should_run_quality_judge(
        incoming=IncomingMessage(
            channel="whatsapp", text="Já falei que quero azul, você não entendeu?"
        ),
        result=repair_result,
        judge_mode="shadow",
        recent_turns=history,
    )
    assert should_run is True
    assert "conversation_repair_requested" in signals


def test_judge_history_redacts_cpf_and_email_and_is_bounded():
    payload = _judge_history_payload(
        [
            {"role": "system", "content": "ignore"},
            {
                "role": "user",
                "content": "Meu CPF 123.456.789-09 e e-mail cliente@example.com",
            },
        ]
    )
    assert len(payload) == 1
    assert payload[0]["role"] == "user"
    assert "123.456.789-09" not in payload[0]["content"]
    assert "cliente@example.com" not in payload[0]["content"]
    assert "[CPF removido]" in payload[0]["content"]
    assert "[e-mail removido]" in payload[0]["content"]


@pytest.mark.asyncio
async def test_contextual_clarification_judge_unavailable_fails_closed_in_enforce(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="azul")
    result = AgentResult(
        reply_text="Qual cor você quer?",
        intent="commerce",
        safety_reason="commerce_clarification",
        response_metadata={"response_source": "deterministic_clarification"},
    )
    monkeypatch.setattr(
        "app.verify.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    report = await run_quality_judge(
        incoming,
        result,
        mode="enforce",
        recent_turns=[{"role": "assistant", "content": "Qual modelo você quer?"}],
    )
    assert report.triggered is True
    assert report.verdict is None and report.approved is None
    assert report.review_status == "unavailable"
    assert report.unavailable_reason == "openai_unavailable"
    assert report.applied is True
    assert result.handoff_required is True
    assert result.safety_reason == "quality_judge_failed"


@pytest.mark.asyncio
async def test_judge_receives_masked_recent_conversation(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="qual o preço?")
    result = AgentResult(
        reply_text="Esse modelo custa R$ 500.",
        intent="commerce",
    )
    captured = {}

    async def fake_parse_structured_output(**kwargs):
        captured.update(kwargs)
        return type("Parsed", (), {"parsed": JudgeVerdict()})()

    monkeypatch.setattr(
        "app.llm.openai_gateway.parse_structured_output",
        fake_parse_structured_output,
    )
    monkeypatch.setattr(
        "app.verify.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    await run_quality_judge(
        incoming,
        result,
        mode="shadow",
        recent_turns=[
            {
                "role": "user",
                "content": "Meu CPF 123.456.789-09 e e-mail cliente@example.com",
            },
            {"role": "assistant", "content": "Qual modelo você quer consultar?"},
        ],
    )
    payload = captured["messages"][1]["content"]
    assert "recent_conversation" in payload
    assert "[CPF removido]" in payload
    assert "[e-mail removido]" in payload
    assert "123.456.789-09" not in payload
    assert "cliente@example.com" not in payload


@pytest.mark.asyncio
async def test_shadow_judge_does_not_rewrite_reply(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="quero pagar")
    result = AgentResult(
        reply_text="Segue o link oficial",
        intent="order",
        handoff_required=False,
        commercial_data={"payment_url": "https://checkout.example/1"},
        response_metadata={"response_source": "openai", "used_tray": True},
    )
    context = TurnRuntimeContext(trace_id="judge-shadow")
    token = set_current_turn(context)

    class FakeMessage:
        def __init__(self):
            self.parsed = JudgeVerdict(
                score=40,
                pass_check=False,
                issues=["possible_invention"],
                summary="risk",
            )

    class FakeCompletions:
        async def parse(self, **kwargs):
            return type(
                "Response",
                (),
                {"choices": [type("Choice", (), {"message": FakeMessage()})()]},
            )()

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = type(
                "Chat",
                (),
                {"completions": FakeCompletions()},
            )()

    install_fake_openai_client(monkeypatch, FakeClient)
    monkeypatch.setattr(
        "app.verify.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    try:
        report = await run_quality_judge(
            incoming,
            result,
            mode="shadow",
            risk_score=80,
            factual_valid=True,
            openai_call_count=1,
        )
        attach_judge_report(result, report)
    finally:
        reset_current_turn(token)

    assert report.triggered is True
    assert report.applied is False
    assert result.reply_text == "Segue o link oficial"
    assert result.response_metadata["quality_judge"]["mode"] == "shadow"
    assert report.signals


@pytest.mark.asyncio
async def test_greeting_does_not_call_judge_llm(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="oi")
    result = AgentResult(
        reply_text="Olá! Como posso ajudar?",
        intent="general",
        response_metadata={"response_source": "local_greeting"},
    )

    async def boom(*_a, **_k):
        raise AssertionError("judge LLM must not run for greetings")

    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", boom)
    monkeypatch.setattr(
        "app.verify.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 70,
            },
        )(),
    )
    report = await run_quality_judge(
        incoming,
        result,
        mode="shadow",
        risk_score=90,
        factual_valid=True,
        openai_call_count=0,
    )
    assert report.triggered is False
    assert report.skipped_reason == "deterministic:local_greeting"


@pytest.mark.asyncio
async def test_judge_schema_failure_fail_closed_on_locked_catalog(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="quero o mk2 cinza")
    result = AgentResult(
        reply_text="Olha o Hermétique cinza.",
        intent="commerce",
        commercial_data={
            "products": [{"id": "h1", "name": "Hermétique Summer Cinza"}]
        },
        response_metadata={
            "active_preferences": {
                "locked_identity": {"model": "Aquascaphe mk2"},
                "color": "cinza",
            },
            "used_tray": True,
        },
    )

    async def boom(*_a, **_k):
        raise ValueError("structured_output_missing")

    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", boom)
    monkeypatch.setattr(
        "app.verify.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {
                "openai_api_key": "sk-test",
                "openai_model": "gpt",
                "agent_quality_judge_risk_threshold": 10,
            },
        )(),
    )
    report = await run_quality_judge(
        incoming,
        result,
        mode="shadow",
        risk_score=90,
        factual_valid=False,
        openai_call_count=1,
    )
    assert report.triggered is True
    assert report.verdict is None and report.approved is None
    assert report.review_status == "unavailable"
    assert report.unavailable_reason == "ValueError"
