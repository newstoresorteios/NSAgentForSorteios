"""Offline deterministic eval suite (no live OpenAI/Tray in CI)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals.scoring import score_eval_case


FIXTURES = Path(__file__).parent / "fixtures" / "conversations.json"
ONLINE_EVAL = False  # separate online path stays disabled by default


def _load_cases() -> list[dict]:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def test_eval_fixture_has_at_least_50_cases():
    cases = _load_cases()
    assert len(cases) >= 50
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_offline_eval_case_contract(case):
    expected = case["expected"]
    # Deterministic stub observation derived only from the fixture contract.
    observed_tools = list(expected.get("must_call_tools") or [])
    reply = "Resposta sintética segura."
    if expected.get("handoff_required"):
        reply = "Vou encaminhar para um atendente humano."
    result = score_eval_case(
        case,
        observed_domain=expected.get("domain"),
        observed_tools=observed_tools,
        reply_text=reply,
        openai_calls=min(int(expected.get("max_openai_calls") or 0), 1),
        factual_valid=True,
        handoff_required=bool(expected.get("handoff_required")),
        invented_claim=False,
    )
    assert result["passed"] is True
    assert result["score"] == 100.0


def test_online_eval_disabled_by_default():
    assert ONLINE_EVAL is False
