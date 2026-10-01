"""Context review exercised through the real pipeline with offline model boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import AgentResult, IncomingMessage
from app.ops.runtime_context import reset_current_turn, set_current_turn
from app.ops.turn_runtime import LLMCallBudget, TurnRuntimeContext
from app.verify.quality_judge import JudgeVerdict, run_quality_judge
from app.verify.response_critique import CritiqueVerdict, apply_response_critique_loop


HISTORY = [{"role": "assistant", "content": "Qual cor de relógio você prefere?"}]


def clarification():
    return AgentResult(
        reply_text=HISTORY[0]["content"], intent="commerce", safety_reason="commerce_clarification",
        response_metadata={"response_source": "deterministic_clarification", "domain": "commerce"},
    )


def settings():
    return SimpleNamespace(
        audio_inbound_enabled=False, audio_outbound_enabled=False,
        agent_policy_mode="shadow", agent_factual_validation_mode="enforce",
        agent_trusted_fact_domains="", agent_critique_mode="shadow",
        agent_quality_judge_mode="shadow", agent_double_check_mode="off",
        agent_emergency_rollback=False, max_reply_chars=900, agent_persona_tenant_id="newstore",
        agent_max_llm_calls_per_turn=2, agent_max_llm_calls_per_turn_complex=4,
        agent_llm_budget_enabled=True, agent_critique_max_retries=1,
        agent_critique_llm_on_risk_only=True, agent_critique_enforce_on_commerce=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_question", [True, False])
async def test_pipeline_reviews_each_draft_once_and_repairs_answered_question(monkeypatch, repeated_question):
    import app.message_pipeline as pipeline

    cfg = settings()
    runtime = TurnRuntimeContext(trace_id="context-review", llm_budget=LLMCallBudget(max_calls=2, enforce=True))
    token = set_current_turn(runtime)
    reviews = []

    async def generate(*_args, **_kwargs):
        runtime.register_openai_call("decision")
        result = clarification()
        if not repeated_question:
            result.reply_text = "Qual modelo você quer consultar?"
        return result

    async def judge(**kwargs):
        runtime.register_openai_call("judge")
        reviews.append(kwargs)
        assert kwargs["recent_turns"] == HISTORY
        if not repeated_question:
            return CritiqueVerdict(pass_check=True, dimensions={"context_use": True, "progress": True})
        if len(reviews) == 1:
            return CritiqueVerdict(
                pass_check=False, issues=["repeated_answered_question"],
                dimensions={"context_use": False, "progress": False},
            )
        assert "azul" in kwargs["result"].reply_text
        return CritiqueVerdict(pass_check=True, dimensions={"context_use": True, "progress": True})

    async def regenerate(**kwargs):
        runtime.register_openai_call("response_composition")
        fixed = kwargs["result"].model_copy(deep=True)
        fixed.reply_text = "Você informou azul. Vou considerar essa cor nas opções."
        fixed.safety_reason = None
        return fixed

    async def keep(result, **_kwargs):
        return result, None, None

    def factual(result, **_kwargs):
        result.response_metadata["factual_validation"] = {"valid": True}
        return result

    monkeypatch.setattr(pipeline, "get_settings", lambda: cfg)
    monkeypatch.setattr("app.llm.llm_call_policy.get_settings", lambda: cfg)
    monkeypatch.setattr(pipeline, "load_commerce_conversation_state", lambda **_kwargs: {})
    monkeypatch.setattr(pipeline, "persist_customer_commerce_session", lambda **_kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_customer_identity_links", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "generate_agent_reply_async", generate)
    monkeypatch.setattr(pipeline, "apply_factual_validation", factual)
    monkeypatch.setattr(pipeline, "compose_outbound_reply", lambda incoming, result, **_kwargs: result)
    monkeypatch.setattr("app.sales.scope_send_gate.apply_scope_send_gate_with_retry", keep)
    monkeypatch.setattr("app.sales.answer_council.apply_answer_council_with_retry", keep)
    monkeypatch.setattr("app.verify.outbound_compliance.apply_outbound_compliance", lambda **kwargs: (kwargs["result"], None))
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    monkeypatch.setattr("app.verify.response_critique._regenerate_reply", regenerate)
    duplicate = AsyncMock(side_effect=AssertionError("A reviewed draft must not consume a second judge"))
    monkeypatch.setattr(pipeline, "run_quality_judge", duplicate)
    try:
        result = await pipeline._process_incoming_message(
            IncomingMessage(text="azul", conversation_id="offline-context"),
            {"_model_conversation_turns": HISTORY, "_conversation_turns": HISTORY},
        )
    finally:
        reset_current_turn(token)
    assert result.response_metadata["response_critique"]["approved"] is True
    if repeated_question:
        assert len(reviews) == 2  # First draft, then the regenerated draft.
        assert runtime.llm_calls_by_type == {"decision": 1, "judge": 2, "response_composition": 1}
        assert runtime.llm_budget.used_calls == runtime.llm_budget.max_calls == 4
        assert result.response_metadata["response_critique"]["mode"] == "enforce"
        assert result.response_metadata["response_critique"]["regenerated"] is True
        assert "azul" in result.reply_text and not result.handoff_required
    else:
        assert len(reviews) == 1
        assert runtime.llm_calls_by_type == {"decision": 1, "judge": 1}
        assert result.response_metadata["response_critique"]["mode"] == "shadow"
        assert result.reply_text.endswith("Qual modelo você quer consultar?")
    duplicate.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_repair_budget_is_checked_before_spending_on_review(monkeypatch):
    cfg = settings()
    monkeypatch.setattr("app.llm.llm_call_policy.get_settings", lambda: cfg)
    runtime = TurnRuntimeContext(trace_id="review-budget", llm_budget=LLMCallBudget(max_calls=4, used_calls=4, enforce=True))
    token = set_current_turn(runtime)
    judge = AsyncMock(side_effect=AssertionError("Do not spend the last repair slots on an initial review"))
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    try:
        result, report = await apply_response_critique_loop(
            incoming=IncomingMessage(text="azul"), result=clarification(), recent_turns=HISTORY,
            mode="shadow", max_retries=1,
        )
    finally:
        reset_current_turn(token)
    assert report.mode == "enforce" and report.critical_context
    assert report.planned_review_calls == ["judge", "response_composition", "judge"]
    assert report.approved is None and report.review_status == "unavailable"
    assert report.unavailable_reason == "complete_review_budget_unavailable"
    assert result.handoff_required and report.applied_handoff
    assert runtime.llm_budget.used_calls == 4
    assert result.response_metadata["quality_judge"]["verdict"] is None
    judge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_available_first_review_is_used_without_exceeding_repair_cap(monkeypatch, approved):
    cfg = settings()
    monkeypatch.setattr("app.llm.llm_call_policy.get_settings", lambda: cfg)
    runtime = TurnRuntimeContext(trace_id="limited-review", llm_budget=LLMCallBudget(max_calls=4, used_calls=2, enforce=True))
    token = set_current_turn(runtime)
    async def judge(**kwargs):
        runtime.register_openai_call("judge")
        return CritiqueVerdict(pass_check=approved,
            dimensions={"relevance": approved, "context_use": approved, "progress": approved})
    regeneration = AsyncMock(side_effect=AssertionError("Repair and its review do not fit the cap"))
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    monkeypatch.setattr("app.verify.response_critique._regenerate_reply", regeneration)
    try:
        result, report = await apply_response_critique_loop(
            incoming=IncomingMessage(text="azul"), result=clarification(), recent_turns=HISTORY,
            mode="enforce", max_retries=1,
        )
    finally:
        reset_current_turn(token)
    assert runtime.llm_budget.used_calls == 3
    assert report.planned_review_calls == ["judge"] and report.repair_budget_unavailable
    assert not report.regenerated
    if approved:
        assert report.approved is True and report.review_status == "approved"
        assert not result.handoff_required
    else:
        assert report.approved is None and report.review_status == "unavailable"
        assert result.handoff_required
        assert report.verdicts[0]["pass_check"] is False
    regeneration.assert_not_awaited()


def test_known_failed_review_does_not_reuse_the_rejected_catalog_reply():
    from app.verify.response_critique import CritiqueLoopReport, _handle_unavailable_review
    result = AgentResult(reply_text="Preço incorreto.", intent="commerce",
        commercial_data={"products": [{"id": "synthetic"}]},
        response_metadata={"factual_validation": {"valid": True}})
    report = CritiqueLoopReport(mode="enforce", verdicts=[{
        "pass_check": False, "dimensions": {"factuality": False}, "issues": ["price_mismatch"]}])
    final = _handle_unavailable_review(result, report, "repair_budget_unavailable")
    assert final.handoff_required and final.reply_text != "Preço incorreto."
    assert report.approved is None


@pytest.mark.asyncio
async def test_ordinary_clarification_is_reviewed_in_shadow_and_skips_are_not_passes(monkeypatch):
    cfg = settings()
    monkeypatch.setattr("app.llm.llm_call_policy.get_settings", lambda: cfg)
    judge = AsyncMock(return_value=CritiqueVerdict(pass_check=True))
    monkeypatch.setattr("app.verify.response_critique.run_critique_judge", judge)
    result, report = await apply_response_critique_loop(
        incoming=IncomingMessage(text="azul"),
        result=AgentResult(reply_text="Qual modelo você quer?", intent="commerce", safety_reason="commerce_clarification",
                           response_metadata={"response_source": "deterministic_clarification"}),
        recent_turns=HISTORY, mode="shadow",
    )
    assert report.mode == "shadow" and report.approved is True
    assert judge.await_count == 1 and not result.handoff_required
    _, skipped = await apply_response_critique_loop(
        incoming=IncomingMessage(text="oi"), result=AgentResult(reply_text="Olá!", intent="greeting"), mode="shadow",
    )
    assert skipped.review_status == "skipped" and skipped.approved is None


@pytest.mark.asyncio
async def test_unavailable_noncritical_judge_never_returns_approval(monkeypatch):
    monkeypatch.setattr("app.verify.quality_judge.get_settings", lambda: SimpleNamespace(
        openai_api_key="", agent_quality_judge_risk_threshold=70,
    ))
    result = AgentResult(reply_text="Esse relógio custa R$ 500.", intent="commerce")
    report = await run_quality_judge(IncomingMessage(text="quanto custa?"), result, mode="shadow")
    assert report.triggered and report.verdict is None and report.approved is None
    assert report.review_status == "unavailable"
    assert not result.handoff_required


@pytest.mark.parametrize("verdict_type", [JudgeVerdict, CritiqueVerdict])
def test_high_factual_score_cannot_override_failed_context_dimension(verdict_type):
    verdict = verdict_type(score=95, pass_check=True, dimensions={"factuality": True, "context_use": False})
    assert verdict.pass_check is False
    assert verdict.dimensions.delivery is None


@pytest.mark.parametrize("critical", [False, True])
def test_unavailable_review_preserves_grounded_listing_but_never_known_context_failure(critical):
    from app.verify.response_critique import CritiqueLoopReport, _handle_unavailable_review

    result = AgentResult(reply_text="Listagem pública consultada; estoque físico depende de confirmação.",
        intent="commerce", response_metadata={"response_source": "ready_delivery_storefront",
            "factual_validation": {"valid": True}, "ready_delivery_check": {
                "complete": True, "stock_confirmed": False, "products": [{"reference": "fixture"}]}})
    report = CritiqueLoopReport(mode="enforce", critical_context=critical)
    if critical:
        report.verdicts = [{"pass_check": False, "dimensions": {"context_use": False}}]
    result = _handle_unavailable_review(result, report, "offline")
    assert report.approved is None and report.review_status == "unavailable"
    assert bool(result.handoff_required) is critical
    if not critical:
        assert result.reply_text.startswith("Listagem pública consultada")
