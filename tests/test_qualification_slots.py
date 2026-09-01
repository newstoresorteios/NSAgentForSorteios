"""Unit tests for persona qualification slot tracking (João loop fix)."""

from __future__ import annotations

from app.models import SalesInterpretation
from app.persona_runtime import (
    build_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.preference_normalize import normalize_sales_interpretation
from app.sales.discovery import _persona_qualification_question
from app.sales.qualification_slots import (
    CUSTOMER_NAME,
    SHIPPING_CITY,
    URGENCY,
    apply_qualification_slot_answer,
    classify_qualification_question,
    covered_qualification_dims,
    rehydrate_qualification_slots_from_turns,
)


def _clarification_turn(content: str) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "metadata": {"safety_reason": "commerce_clarification"},
    }


def test_classify_qualification_question_slots():
    assert classify_qualification_question("Como posso te chamar?") == CUSTOMER_NAME
    assert classify_qualification_question("Para qual cidade seria a entrega?") == SHIPPING_CITY
    assert (
        classify_qualification_question(
            "Você tem pressa para receber ou pode esperar uma peça sob encomenda?"
        )
        == URGENCY
    )


def test_city_answer_persists_separate_from_name():
    base = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    updated = apply_qualification_slot_answer(
        base,
        SHIPPING_CITY,
        "Florianópolis",
    )
    dims = covered_qualification_dims(updated)
    assert SHIPPING_CITY in dims
    assert CUSTOMER_NAME not in dims
    assert "qual:city:Florianópolis" in list(updated.preferences.attributes)


def test_rehydrate_turns_replays_joao_sequence():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={"budget_max": 10000},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    turns = [
        _clarification_turn("Para qual cidade seria a entrega?"),
        {"role": "user", "content": "Florianópolis"},
        _clarification_turn("Como posso te chamar?"),
        {"role": "user", "content": "João"},
    ]
    updated = rehydrate_qualification_slots_from_turns(interpretation, turns)
    dims = covered_qualification_dims(updated)
    assert SHIPPING_CITY in dims
    assert CUSTOMER_NAME in dims
    assert updated.preferences.recipient == "João"


def test_rehydrate_name_without_clarification_metadata():
    interpretation = SalesInterpretation(
        domain="greeting",
        goal=None,
        subject={},
        preferences={},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    turns = [
        {"role": "user", "content": "quero um relogio"},
        {"role": "assistant", "content": "Claro — como posso te chamar?"},
    ]
    updated = rehydrate_qualification_slots_from_turns(
        interpretation,
        turns,
        message_text="Tironi",
    )
    assert updated.preferences.recipient == "Tironi"
    assert CUSTOMER_NAME in covered_qualification_dims(updated)


def test_continue_commerce_when_name_answer_misread_as_greeting():
    from app.sales.qualification_slots import continue_commerce_from_qualification_answer

    interpretation = SalesInterpretation(
        domain="greeting",
        goal=None,
        subject={},
        preferences={},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    turns = [
        {"role": "assistant", "content": "Claro — como posso te chamar?"},
    ]
    updated = continue_commerce_from_qualification_answer(
        interpretation,
        turns,
        "Tironi",
    )
    assert updated.domain == "commerce"
    assert updated.preferences.recipient == "Tironi"
    assert updated.subject.product_type == "relógio"


def test_persona_question_never_reasks_city_or_name_after_answered():
    from tests.evals.test_sales_golden_backtests import _crono_chatbo_profile, _persona

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile={
            **_crono_chatbo_profile(),
            "qualification_rules": [
                "Para qual cidade seria a entrega?",
                "Você já tem um modelo em mente ou quer uma sugestão?",
                "Qual faixa de investimento você tem em mente?",
                "Você tem pressa para receber ou pode esperar uma peça sob encomenda?",
                "Como posso te chamar?",
            ],
        },
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"brand": "Baltic", "model": "mk2", "product_type": "relógio"},
            preferences={
                "budget_max": 10000,
                "attributes": [
                    "qual:city:Florianópolis",
                    "qual:name:João",
                    "qual:urgency:can_wait",
                ],
                "recipient": "João",
            },
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            needs_clarification=False,
            confidence=0.95,
        )
        import app.sales_agent as sales_agent

        state = sales_agent._discovery_state(interpretation, [])
        question = _persona_qualification_question(interpretation, state)
        if question:
            folded = question.casefold()
            assert "cidade" not in folded
            assert "chamar" not in folded
    finally:
        reset_persona_runtime(token)


def test_baltic_mk2_37mm_skips_budget_and_forces_retrieval():
    from tests.evals.test_sales_golden_backtests import _crono_chatbo_profile, _persona

    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile=_crono_chatbo_profile(),
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"brand": "Baltic", "product_type": "relógio"},
            preferences={},
            references_previous_context=True,
            needs_clarification=True,
            confidence=0.9,
        )
        normalized = normalize_sales_interpretation(
            interpretation,
            message_text="Que o baltic mk2 37mm",
            context_text="Quero o Baltic",
        )
        assert normalized.subject.brand == "Baltic"
        assert "mk2" in (normalized.subject.model or "").casefold()
        assert any("case_size:37" in str(item) for item in normalized.preferences.attributes)
        assert normalized.stop_clarification is True
        assert normalized.ready_for_retrieval is True

        import app.sales_agent as sales_agent

        state = sales_agent._discovery_state(
            normalized,
            [],
            message_text="Que o baltic mk2 37mm",
        )
        assert state["persona_qualification_required"] is False
        assert state["force_retrieval"] is True
        question = _persona_qualification_question(normalized, state)
        assert question is None or "investimento" not in (question or "").casefold()
    finally:
        reset_persona_runtime(token)
