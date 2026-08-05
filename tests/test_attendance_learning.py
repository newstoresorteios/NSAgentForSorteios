"""Unit tests for attendance learning classifiers (no DB)."""

from app.attendance_learning import classify_attendance


def test_classify_preference_misread_empty_catalog():
    row = {
        "customer_text": "feminino até 3000 reais",
        "agent_reply": "Não encontrei esse produto no catálogo agora.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "empty_catalog"
    assert "preference_misread" in result["failure_codes"]


def test_classify_trade_in_policy_miss():
    row = {
        "customer_text": "vcs estão comprando Certina seminovo?",
        "agent_reply": "Não compramos relógios seminovos, apenas vendemos produtos novos.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "policy_miss"
    assert "trade_in_policy_miss" in result["failure_codes"]
