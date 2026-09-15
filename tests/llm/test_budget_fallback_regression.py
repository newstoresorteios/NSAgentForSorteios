from types import SimpleNamespace

import pytest
from app.llm.openai_gateway import FallbackOpenAIGateway, CanaryOpenAIGateway
from app.ops.runtime_context import set_current_turn, reset_current_turn, get_current_turn
from app.ops.turn_runtime import LLMCallBudget, LLMCallBudgetExceeded, TurnRuntimeContext


@pytest.mark.asyncio
@pytest.mark.parametrize("canary", [False, True])
@pytest.mark.parametrize("method", ["generate_text", "parse_structured"])
@pytest.mark.parametrize("max_calls", [0, 1])
async def test_exhausted_budget_never_falls_back(canary, method, max_calls):
    transports = []
    class Gateway:
        async def generate_text(self, **kw):
            get_current_turn().register_openai_call(kw["call_type"])
            transports.append(kw["call_type"])
            return SimpleNamespace(api_mode="", latency_ms=0)
        parse_structured = generate_text
    runtime = TurnRuntimeContext(trace_id="budget", llm_budget=LLMCallBudget(max_calls=max_calls, enforce=True))
    if max_calls:
        runtime.register_openai_call("decision")
    if canary:
        gateway = CanaryOpenAIGateway(chat=Gateway(), responses=Gateway())
        gateway._resolve_gateways = lambda: (gateway._responses, "canary_responses")
    else:
        gateway = FallbackOpenAIGateway(primary=Gateway(), fallback=Gateway())
    token = set_current_turn(runtime)
    try:
        args = {"model": "fake", "call_type": "judge"}
        if method == "parse_structured":
            args["text_format"] = SimpleNamespace
        with pytest.raises(LLMCallBudgetExceeded):
            await getattr(gateway, method)(**args)
        assert transports == []
        assert runtime.logical_llm_calls == max_calls
        assert runtime.openai_transport_attempts == max_calls
        assert not runtime.openai_api_fallback
    finally:
        reset_current_turn(token)


def test_refund_does_not_consume_previous_or_different_reservation():
    runtime = TurnRuntimeContext(trace_id="refund")
    runtime.register_openai_call("decision")
    checkpoint = runtime.openai_transport_attempts
    runtime.release_failed_openai_attempt("judge", after_attempt=0)
    runtime.release_failed_openai_attempt("decision", after_attempt=checkpoint)
    assert runtime.logical_llm_calls == 1
    runtime.register_openai_call("judge")
    runtime.release_failed_openai_attempt("judge", after_attempt=checkpoint)
    assert runtime.logical_llm_calls == 1
    assert runtime.llm_calls_by_type == {"decision": 1}
    assert runtime.openai_transport_attempts == 2
