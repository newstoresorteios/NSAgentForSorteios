"""Deterministic shortlist purchase selection."""

from __future__ import annotations

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import SalesInterpretation
from app.sales.purchase_selection import (
    blocks_persona_qualification_for_purchase,
    is_bare_purchase_closing,
    is_checkout_utterance,
    parse_list_position_selection,
    repair_presented_purchase_selection,
    skips_discovery_clarification,
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


def _seiko_shortlist_state(**overrides) -> CommerceConversationState:
    payload = {
        "active_domain": "commerce",
        "dialogue_phase": "shortlist",
        "last_presented_products": [
            {
                "position": 1,
                "product_id": "3901",
                "name": "Seiko Prospex Alpinist",
                "brand": "Seiko",
                "reference": "SPB155J1",
            },
            {
                "position": 2,
                "product_id": "3917",
                "name": "Seiko Prospex Land Tortoise SRPG15K1",
                "brand": "Seiko",
                "reference": "SRPG15K1",
            },
            {
                "position": 3,
                "product_id": "3903",
                "name": "Seiko Prospex Turtle",
                "brand": "Seiko",
                "reference": "SRPE93K1",
            },
        ],
        "purchase_stage": "selection",
    }
    payload.update(overrides)
    return CommerceConversationState(**payload)


def test_checkout_utterance_covers_fechar_a_compra():
    assert is_checkout_utterance("como podes fazer pra fechar a compra?")
    assert is_checkout_utterance("fechar a compra")
    assert is_checkout_utterance("como posso fechar o negócio?")
    assert not is_checkout_utterance("quero comprar um relógio")
    assert not is_checkout_utterance("procuro um seiko")
    assert not is_checkout_utterance("Eu quero o relógio seiko")


def test_repair_fechar_a_compra_binds_active_inspected_sku():
    state = _seiko_shortlist_state(
        active_product={
            "product_id": "3917",
            "name": "Seiko Prospex Land Tortoise SRPG15K1",
            "brand": "Seiko",
            "reference": "SRPG15K1",
        }
    )
    repaired = repair_presented_purchase_selection(
        _interp(goal="buy", needs_clarification=True),
        message_text="como podes fazer pra fechar a compra?",
        state=state,
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.stop_clarification is True
    assert repaired.needs_clarification is False
    assert repaired.reference_type == "current_product"
    assert skips_discovery_clarification(repaired)
    assert blocks_persona_qualification_for_purchase(repaired, state)


def test_repair_quero_o_seiko_binds_active_same_brand():
    state = _seiko_shortlist_state(
        active_product={
            "product_id": "3917",
            "name": "Seiko Prospex Land Tortoise SRPG15K1",
            "brand": "Seiko",
            "reference": "SRPG15K1",
        }
    )
    repaired = repair_presented_purchase_selection(
        _interp(goal="buy"),
        message_text="Eu quero o relógio seiko",
        state=state,
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_position == 2
    assert skips_discovery_clarification(repaired)


def test_repair_brand_only_without_active_asks_which_option():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="Eu quero o relógio seiko",
        state=_seiko_shortlist_state(),
    )
    assert repaired is not None
    assert repaired.purchase_action is None
    assert repaired.active_topic == "purchase_option_choice"
    assert "Qual opção" in (repaired.clarification_question or "")
    assert "marca" not in (repaired.clarification_question or "").casefold()
    assert not skips_discovery_clarification(repaired)


def test_repair_unique_reference_creates_cart():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="SRPG15K1",
        state=_seiko_shortlist_state(),
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_position == 2


def test_repair_named_land_tortoise_creates_cart():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="quero o land tortoise",
        state=_seiko_shortlist_state(),
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.reference_position == 2


def test_repair_active_only_without_shortlist_still_closes():
    state = CommerceConversationState(
        active_domain="commerce",
        active_product={
            "product_id": "3917",
            "name": "Seiko Prospex Land Tortoise SRPG15K1",
            "brand": "Seiko",
            "reference": "SRPG15K1",
        },
    )
    repaired = repair_presented_purchase_selection(
        _interp(goal="buy"),
        message_text="como podes fazer pra fechar a compra?",
        state=state,
    )
    assert repaired is not None
    assert repaired.purchase_action == "create_cart"
    assert repaired.stop_clarification is True
    assert repaired.needs_clarification is False


def test_repair_open_browse_does_not_bind_previous_shortlist():
    original = _interp(goal="discover", subject={"brand": "Seiko"})
    repaired = repair_presented_purchase_selection(
        original,
        message_text="quero ver um modelo de seiko",
        state=_seiko_shortlist_state(
            active_product={"product_id": "3917", "name": "Land Tortoise", "brand": "Seiko"}
        ),
    )
    assert repaired is original
    assert repaired.purchase_action is None


def test_repair_image_request_does_not_create_cart():
    original = _interp(image_request=True, goal="inspect")
    repaired = repair_presented_purchase_selection(
        original,
        message_text="que pedi a imagem",
        state=_seiko_shortlist_state(
            active_product={"product_id": "3917", "name": "Land Tortoise", "brand": "Seiko"}
        ),
    )
    assert repaired is original


def test_repair_baltic_brand_only_asks_which_not_marca():
    repaired = repair_presented_purchase_selection(
        _interp(),
        message_text="Quero o Baltic",
        state=_presented_state(),
    )
    assert repaired is not None
    assert repaired.active_topic == "purchase_option_choice"
    assert "Qual opção" in (repaired.clarification_question or "")
    assert "marca" not in (repaired.clarification_question or "").casefold()


def test_incident_thread_close_after_inspect_does_not_requalify():
    """Replay the Seiko inspect → close turns without embedding a phone."""
    after_inspect = _seiko_shortlist_state(
        active_product={
            "product_id": "3917",
            "name": "Seiko Prospex Land Tortoise SRPG15K1",
            "brand": "Seiko",
            "reference": "SRPG15K1",
        }
    )
    close = repair_presented_purchase_selection(
        _interp(goal="buy", needs_clarification=True),
        message_text="como podes fazer pra fechar a compra?",
        state=after_inspect,
    )
    assert close is not None
    assert close.purchase_action == "create_cart"
    assert skips_discovery_clarification(close)

    named = repair_presented_purchase_selection(
        _interp(goal="buy"),
        message_text="Eu quero o relógio seiko",
        state=after_inspect,
    )
    assert named is not None
    assert named.purchase_action == "create_cart"
    assert named.reference_position == 2

    image = repair_presented_purchase_selection(
        _interp(image_request=True, goal="inspect"),
        message_text="que pedi a imagem",
        state=after_inspect,
    )
    assert image is not None
    assert image.purchase_action is None
    assert image.image_request is True
