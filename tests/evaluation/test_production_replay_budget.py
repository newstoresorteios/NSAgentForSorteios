from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,limit", [(True, 2), (True, 3), (False, 0)])
async def test_replay_uses_production_budget_policy(monkeypatch, enabled, limit):
    from app.evaluation.runner import replay_case
    from app.llm import llm_call_policy
    from app.ops.runtime_context import get_current_turn
    from app.models import AgentResult

    settings = SimpleNamespace(agent_llm_budget_enabled=enabled, agent_max_llm_calls_per_turn=limit,
                               agent_max_llm_calls_per_turn_complex=4)
    monkeypatch.setattr(llm_call_policy, "get_settings", lambda: settings)
    monkeypatch.setattr("app.evaluation.runner.bounded", lambda *args: 20)

    async def process(*args):
        budget = get_current_turn().llm_budget
        assert budget.max_calls == limit
        assert budget.enforce == enabled
        return AgentResult(reply_text="ok")

    monkeypatch.setattr("app.message_pipeline.process_incoming_message", process)
    report, _ = await replay_case({"workspace_id": "w", "history": [], "initial_state": {},
                                   "input": "oi", "channel": "whatsapp"}, None)
    assert report["error"] is None
