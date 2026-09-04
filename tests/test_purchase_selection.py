"""Deterministic shortlist purchase selection (João close-the-deal path)."""

from __future__ import annotations

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import SalesInterpretation
from app.sales.purchase_selection import (
    blocks_persona_qualification_for_purchase,
    is_bare_purchase_closing,
    parse_list_position_selection,
    repair_presented_purchase_selection,
)


def _presented_state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "last_presented_products": [
            {
                "position": 1,
                "product_id": "aquascaphe",
                "name": "Baltic Aquascaphe",
                "brand": "Baltic",
            },
            {
                "position": 2,
                "product_id": "sb01",
                "name": "Baltic Classic SB01",
                "brand": "Baltic",
            },
            {
                "position": 3,
                "product_id": "mr01",
                "name": "Baltic MR01",
                "brand": "Baltic",
            },
        ],
        "purchase_stage": "selection",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def _interp(**overrides) -> SalesInterpretation:
    payload = {
        "domain": "commerce",
        "goal": "recommend",
        "subject": {"product_type": "relógio", "brand": "Baltic"},
        "preferences": {"style": "mergulho"},
        "information_needed": ["catalog"],
        "references_previous_context": True,
        "needs_clarification": True,
        "enough_information_to_search": True,
        "ready_for_retrieval": True,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return SalesInterpretation(**payload)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Quero comprar o 2", 2),
        ("quero o 2", 2),
        ("o segundo", 2),
        ("opção 3", 3),
        ("levar o primeiro", 1),
        ("quero um de mergulho", None),
        ("Quero o Baltic", None),
        ("mk2 37mm", None),
    ],
)
def test_parse_list_position_selection(text, expected):
    assert parse_list_position_selection(text) == expected


def test_bare_purchase_closing_phrases():
    assert is_bare_purchase_closing("Quero comprar")
    assert is_bare_purchase_closing("quero fechar")
    assert is_bare_purchase_closing("Quero comprar o 2")
    assert not is_bare_purchase_closing("quero um de mergulho")
    assert not is_bare_purchase_closing("Quero o Baltic")


def test_repair_comprar_o_2_forces_create_cart():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="Quero comprar o 2",
        state=_presented_state(),
    )
    assert repaired is not None
    assert repaired.goal == "buy"
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_type == "list_position"
    assert repaired.reference_position == 2
    assert repaired.needs_clarification is False
    assert repaired.stop_clarification is True
    assert blocks_persona_qualification_for_purchase(repaired, _presented_state())


def test_repair_bare_quero_comprar_uses_prior_position():
    recent = [
        {"role": "user", "content": "Quero comprar o 2"},
        {
            "role": "assistant",
            "content": "Sim, encontrei: 1. Aquascaphe 2. Classic SB01 3. MR01",
        },
        {"role": "user", "content": "Quero comprar"},
    ]
    # Prior user turn already said "o 2"; bare close should reuse it.
    # Note: current message is "Quero comprar" — recent includes that too,
    # so scan finds "Quero comprar o 2" first when walking reverse... 
    # Wait, reverse: last is "Quero comprar" (no pos), then assistant, then
    # "Quero comprar o 2" → position 2.
    repaired = repair_presented_purchase_selection(
        _interp(goal="buy"),
        message_text="Quero comprar",
        state=_presented_state(),
        recent_turns=recent,
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_position == 2


def test_repair_bare_quero_comprar_without_position_asks_which():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="Quero comprar",
        state=_presented_state(),
        recent_turns=[],
    )
    assert repaired is not None
    assert repaired.goal == "buy"
    assert repaired.purchase_action is None
    assert repaired.active_topic == "purchase_option_choice"
    assert repaired.needs_clarification is True
    assert "Qual opção" in (repaired.clarification_question or "")
    assert blocks_persona_qualification_for_purchase(repaired, _presented_state())


def test_repair_noop_without_presented_products():
    empty = CommerceConversationState(active_domain="commerce")
    original = _interp()
    repaired = repair_presented_purchase_selection(
        original,
        message_text="Quero comprar o 2",
        state=empty,
    )
    assert repaired is original
