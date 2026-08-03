import pytest

from app.models import AgentResult, IncomingMessage
from app.quality_judge import (
    JudgeVerdict,
    attach_judge_report,
    run_quality_judge,
    should_trigger_judge,
)
from app.runtime_context import reset_current_turn, set_current_turn
from app.turn_runtime import TurnRuntimeContext


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


@pytest.mark.asyncio
async def test_shadow_judge_does_not_rewrite_reply(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="quero pagar")
    result = AgentResult(
        reply_text="Segue o link oficial",
        intent="order",
        handoff_required=False,
        commercial_data={"payment_url": "https://checkout.example/1"},
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

    monkeypatch.setattr("app.quality_judge.AsyncOpenAI", FakeClient)
    monkeypatch.setattr(
        "app.quality_judge.get_settings",
        lambda: type(
            "S",
            (),
            {"openai_api_key": "sk-test", "openai_model": "gpt"},
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
