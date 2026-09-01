from __future__ import annotations

import pytest

from app.observability import (
    get_iq_counters,
    record_close_miss,
    record_dialogue_phase_transition,
    record_scope_mismatch,
    reset_iq_counters,
)
from app.turn_metrics import build_turn_quality_event
from app.turn_runtime import TurnRuntimeContext


@pytest.fixture(autouse=True)
def _reset_counters():
    reset_iq_counters()
    yield
    reset_iq_counters()


def test_iq_counters_increment_and_expose_in_turn_quality():
    runtime = TurnRuntimeContext(
        trace_id="iq-1",
        conversation_key="whatsapp:5511888888888",
        channel="whatsapp",
    )
    from app.runtime_context import reset_current_turn, set_current_turn

    token = set_current_turn(runtime)
    try:
        record_scope_mismatch(reason="all_excluded_brand", channel="whatsapp")
        record_close_miss(reason="purchase_close_hold", channel="whatsapp")
        record_dialogue_phase_transition(
            from_phase="shortlist",
            to_phase="buy",
            channel="whatsapp",
        )
        record_dialogue_phase_transition(
            from_phase="buy",
            to_phase="buy",
            channel="whatsapp",
        )
    finally:
        reset_current_turn(token)

    counters = get_iq_counters()
    assert counters["scope_mismatch"] == 1
    assert counters["close_miss"] == 1
    assert counters["dialogue_phase_transition"] == 1

    runtime.iq_counters = dict(counters)
    event = build_turn_quality_event(runtime, result_metadata={"domain": "commerce"})
    assert event["iq_counters"]["scope_mismatch"] == 1
    assert event["iq_counters"]["close_miss"] == 1
    assert event["iq_counters"]["dialogue_phase_transition"] == 1
    assert "5511888888888" not in str(event)
