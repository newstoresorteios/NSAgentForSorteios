from app.conversation_summary_policy import should_apply_summary_delta
from app.memory_models import ConversationSummaryDelta


def test_empty_or_noise_summary_is_skipped():
    assert should_apply_summary_delta(None) is False
    assert (
        should_apply_summary_delta(
            ConversationSummaryDelta(open_questions=["só uma?"])
        )
        is False
    )
    assert (
        should_apply_summary_delta(
            ConversationSummaryDelta(current_goal="comprar"),
            existing={"current_goal": "comprar"},
        )
        is False
    )


def test_meaningful_summary_deltas_are_applied():
    assert should_apply_summary_delta(
        ConversationSummaryDelta(commitments=["cliente pediu link"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(user_corrections=["não é GMT"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(resolved_points=["marca confirmada"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(last_failure="tray_timeout")
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(current_goal="pagar pedido"),
        existing={"current_goal": "buscar produto"},
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(open_questions=["cor?", "orçamento?"])
    )
