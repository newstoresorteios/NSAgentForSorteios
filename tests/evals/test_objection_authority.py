"""Offline golden tests for deterministic ChatBo objection handlers."""

from __future__ import annotations

import pytest

from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage, SalesInterpretation
from app.persona.persona_models import PersonaVersion
from app.persona.persona_runtime import (
    _as_prompt_list,
    build_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.sales.policies.objection_authority import (
    detect_objection_kind,
    try_objection_authority_result,
)


def _persona() -> PersonaVersion:
    return PersonaVersion.model_validate(
        {
            "id": 20,
            "tenant_id": "newstore",
            "persona_key": "newstore_commercial",
            "version": 20,
            "name": "Crono",
            "instructions": "15% no PIX.",
            "instructions_hash": "obj",
            "status": "active",
            "metadata": {"chatboPersonaId": "11111111-1111-1111-1111-111111111111"},
        }
    )


@pytest.mark.offline_eval
def test_as_prompt_list_reads_chatbo_items_object():
    prompts = _as_prompt_list(
        {
            "items": [
                "Preço: O valor do site já é o preço final.",
                "Prazo: pronta entrega 2 a 5 dias úteis.",
            ]
        }
    )
    assert len(prompts) == 2
    assert prompts[0].startswith("Preço:")


@pytest.mark.offline_eval
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("faz 20% no pix de desconto", "extra_discount"),
        ("ta caro demais", "price"),
        ("qual o prazo de entrega?", "lead_time"),
        ("posso confiar na loja?", "trust"),
        ("vi mais barato em outro site", "comparison"),
        ("preciso falar com minha esposa", "approval"),
        ("vocês estão comprando relógio seminovo?", "trade_in"),
    ],
)
def test_detect_objection_kinds(text: str, kind: str):
    assert detect_objection_kind(text) == kind


@pytest.mark.offline_eval
def test_extra_discount_uses_persona_pix_and_handoff_policy():
    runtime = build_persona_runtime(
        active=_persona(),
        chatbo_profile={
            "name": "Crono New Store",
            "objection_handling": {
                "items": [
                    "Preço: O valor do site já é o preço final.",
                    "Nunca ofereça desconto além dos 15% do PIX — qualquer negociação vai para um consultor humano.",
                ]
            },
            "recommendation_rules": ["Nunca apresentar mais de 3 peças de uma vez"],
        },
    )
    token = set_persona_runtime(runtime)
    try:
        assert len(runtime.objection_prompts) >= 1
        result = try_objection_authority_result(
            IncomingMessage(channel="whatsapp", text="faz 20% no pix"),
            SalesInterpretation(
                domain="commerce",
                goal="buy",
                subject={},
                preferences={},
                information_needed=[],
                references_previous_context=False,
                enough_information_to_search=False,
                ready_for_retrieval=False,
                stop_clarification=False,
                needs_clarification=False,
                confidence=0.8,
            ),
            CommerceConversationState(),
        )
        assert result is not None
        assert result.safety_reason == "objection_extra_discount"
        assert "15%" in result.reply_text
        assert result.handoff_required is True
        assert "consultor" in result.reply_text.casefold()
    finally:
        reset_persona_runtime(token)


@pytest.mark.offline_eval
def test_objection_does_not_steal_checkout_payment_turns():
    result = try_objection_authority_result(
        IncomingMessage(channel="whatsapp", text="ta caro"),
        SalesInterpretation(
            domain="commerce",
            goal="buy",
            subject={},
            preferences={},
            information_needed=[],
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            stop_clarification=False,
            needs_clarification=False,
            confidence=0.9,
            payment_request_kind="checkout",
        ),
        CommerceConversationState(),
    )
    assert result is None
