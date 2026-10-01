from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage
from app.verify import quality_judge, response_critique


def test_delivery_unconfirmed_cannot_fail_a_generation_review():
    for model in (quality_judge.JudgeVerdict, response_critique.CritiqueVerdict):
        verdict = model(pass_check=True, dimensions={"relevance": True, "delivery": False})
        assert verdict.pass_check is True
        verdict = model(pass_check=True, dimensions={"relevance": False, "delivery": True})
        assert verdict.pass_check is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", ["judge", "critique"])
@pytest.mark.parametrize("measured", [False, True])
async def test_contextual_approval_requires_context_dimensions(monkeypatch, reviewer, measured):
    verdict_type = quality_judge.JudgeVerdict if reviewer == "judge" else response_critique.CritiqueVerdict
    dimensions = {"relevance": True, "context_use": True, "progress": True} if measured else {}
    verdict = verdict_type(pass_check=True, score=95, dimensions=dimensions)
    settings = SimpleNamespace(openai_api_key="test", openai_model="fake", agent_quality_judge_risk_threshold=70)
    monkeypatch.setattr(quality_judge, "get_settings", lambda: settings)
    monkeypatch.setattr(response_critique, "get_settings", lambda: settings)
    async def parse(**kwargs): return SimpleNamespace(parsed=verdict)
    monkeypatch.setattr("app.llm.openai_gateway.parse_structured_output", parse)
    incoming = IncomingMessage(text="azul")
    result = AgentResult(reply_text="Qual tamanho você prefere?", intent="commerce",
                         safety_reason="commerce_clarification")
    history = [{"role": "assistant", "content": "Qual cor você prefere?"}]
    if reviewer == "judge":
        report = await quality_judge.run_quality_judge(incoming, result, mode="shadow", recent_turns=history)
        assert report.review_status == ("approved" if measured else "unavailable")
        assert report.approved is (True if measured else None)
    else:
        verdict = await response_critique.run_critique_judge(incoming=incoming, result=result,
            recent_turns=history, commerce_state=None)
        assert verdict._execution_error == (None if measured else "context_review_dimensions_unverified")
