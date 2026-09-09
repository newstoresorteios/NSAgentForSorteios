"""Unit tests for persona qualification slot tracking."""

from __future__ import annotations

from app.models import SalesInterpretation
from app.persona.persona_runtime import (
    build_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)
from app.catalog.specs.preference_normalize import normalize_sales_interpretation
from app.sales.discovery import _persona_qualification_question
from app.commerce.commerce_context import CommerceConversationState
from app.sales.qualification_slots import (
    CUSTOMER_NAME,
    SHIPPING_CITY,
    URGENCY,
    apply_qualification_slot_answer,
    apply_stored_qualification_slots,
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


def test_stored_qualification_slots_survive_without_turns():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    state = CommerceConversationState(
        dialogue_phase="shortlist",
        active_preferences={
            "recipient": "João",
            "attributes": [
                "qual:name:João",
                "qual:city:Florianópolis",
                "qual:urgency:can_wait",
            ],
        },
    )
    updated = apply_stored_qualification_slots(interpretation, state)
    dims = covered_qualification_dims(updated)
    assert CUSTOMER_NAME in dims
    assert SHIPPING_CITY in dims
    assert URGENCY in dims
    assert updated.preferences.recipient == "João"


def test_store_writes_qualification_slots_onto_state():
    from app.sales.qualification_slots import store_qualification_slots_on_state

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={
            "recipient": "João",
            "attributes": [
                "qual:name:João",
                "qual:city:Florianópolis",
                "qual:urgency:can_wait",
            ],
        },
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.9,
    )
    state = CommerceConversationState()
    store_qualification_slots_on_state(state, interpretation)
    slots = (state.active_preferences or {}).get("qualification_slots")
    assert slots[CUSTOMER_NAME] == "João"
    assert slots[SHIPPING_CITY] == "Florianópolis"
    assert slots[URGENCY] == "can_wait"

    blank = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"brand": "Baltic", "product_type": "relógio"},
        preferences={},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    restored = apply_stored_qualification_slots(blank, state)
    assert restored.preferences.recipient == "João"
    assert CUSTOMER_NAME in covered_qualification_dims(restored)
    assert SHIPPING_CITY in covered_qualification_dims(restored)


def test_evolve_keeps_qualification_slots_when_later_dump_omits_them():
    from app.commerce.commerce_context import evolve_commerce_state
    from app.models import AgentResult
    from app.sales.qualification_slots import store_qualification_slots_on_state

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={
            "recipient": "João",
            "attributes": ["qual:name:João", "qual:city:Florianópolis"],
        },
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.9,
    )
    state = CommerceConversationState()
    store_qualification_slots_on_state(state, interpretation)
    evolved = evolve_commerce_state(
        state,
        AgentResult(
            reply_text="Encontrei 3 opções.",
            intent="commerce",
            response_metadata={
                "domain": "commerce",
                "active_preferences": {"budget_max": 10000},
            },
        ),
    )
    slots = (evolved.active_preferences or {}).get("qualification_slots") or {}
    assert slots[CUSTOMER_NAME] == "João"
    assert slots[SHIPPING_CITY] == "Florianópolis"
    assert evolved.active_preferences.get("budget_max") == 10000

    blank = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"brand": "Baltic"},
        preferences={},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    restored = apply_stored_qualification_slots(blank, evolved)
    assert restored.preferences.recipient == "João"
    assert SHIPPING_CITY in covered_qualification_dims(restored)


def test_browse_reset_keeps_qualification_slots():
    from app.sales.dialogue_phase import reset_browse_memory_keep_orders

    state = CommerceConversationState(
        last_presented_products=[
            {"position": 1, "product_id": "P1", "name": "Seiko 5"},
        ],
        active_preferences={
            "budget_max": 4000,
            "locked_identity": {"brand": "Seiko"},
            "qualification_slots": {
                CUSTOMER_NAME: "João",
                SHIPPING_CITY: "Florianópolis",
            },
        },
    )
    reset = reset_browse_memory_keep_orders(state)
    assert reset.last_presented_products == []
    assert "locked_identity" not in (reset.active_preferences or {})
    assert "budget_max" not in (reset.active_preferences or {})
    slots = (reset.active_preferences or {}).get("qualification_slots") or {}
    assert slots[CUSTOMER_NAME] == "João"
    assert slots[SHIPPING_CITY] == "Florianópolis"


def test_mark_sales_result_writes_qualification_slots():
    from app.models import AgentResult
    from app.sales.result_utils import mark_sales_result

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={
            "recipient": "João",
            "attributes": ["qual:name:João", "qual:city:Florianópolis"],
        },
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.9,
    )
    marked = mark_sales_result(
        AgentResult(reply_text="ok", intent="commerce"),
        interpretation=interpretation,
        goal="discover",
        response_source="deterministic_fallback",
        used_openai_responder=False,
        used_tray=False,
    )
    slots = (marked.response_metadata.get("active_preferences") or {}).get(
        "qualification_slots"
    ) or {}
    assert slots[CUSTOMER_NAME] == "João"
    assert slots[SHIPPING_CITY] == "Florianópolis"


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


def test_rehydrate_does_not_apply_stale_city_question_after_new_assistant_turn():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="buy",
        subject={"brand": "Orient", "model": "Open Heart"},
        preferences={},
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.98,
    )
    turns = [
        _clarification_turn("Para qual cidade seria a entrega?"),
        {
            "role": "assistant",
            "content": "Qual opção da lista você quer comprar (1, 2 ou 3)?",
        },
    ]
    updated = rehydrate_qualification_slots_from_turns(
        interpretation,
        turns,
        message_text="o orient open heart preto",
    )
    assert SHIPPING_CITY not in covered_qualification_dims(updated)


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


def test_sou_o_joao_is_name_intro_not_greeting():
    from app.sales.qualification_slots import (
        continue_commerce_from_qualification_answer,
        extract_introduced_name,
    )

    assert extract_introduced_name("Sou o João") == "João"
    assert extract_introduced_name("me chamo Tironi") == "Tironi"
    assert extract_introduced_name("Dark Orange") is None
    interpretation = SalesInterpretation(
        domain="greeting",
        goal=None,
        subject={},
        preferences={},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    updated = continue_commerce_from_qualification_answer(
        interpretation,
        [],
        "Sou o João",
    )
    assert updated.domain == "commerce"
    assert updated.preferences.recipient == "João"


def test_brevo_color_nick_is_not_a_person_name():
    from app.sales.qualification_slots import _is_plausible_name

    assert _is_plausible_name("Dark Orange") is False
    assert _is_plausible_name("Razor Blue") is False
    assert _is_plausible_name("João") is True
    assert _is_plausible_name("Tironi") is True


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


def test_legacy_product_phrase_is_removed_from_persisted_shipping_city():
    from app.sales.qualification_slots import merge_persisted_qualification_slots

    merged = merge_persisted_qualification_slots(
        {"material": "dourado"},
        {
            "qualification_slots": {
                "customer_name": "Tironi",
                "shipping_city": "o orient open heart preto",
            },
            "attributes": [
                "qual:name:Tironi",
                "qual:city:o orient open heart preto",
            ],
        },
    )

    assert merged["qualification_slots"] == {"customer_name": "Tironi"}
    assert "qual:city:o orient open heart preto" not in merged["attributes"]


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


def test_commerce_browse_phrase_is_not_a_name_slot_answer():
    from app.sales.qualification_slots import is_qualification_slot_answer

    turns = [{"role": "assistant", "content": "Claro — como posso te chamar?"}]
    assert is_qualification_slot_answer(turns, "Tironi") is True
    assert is_qualification_slot_answer(turns, "quero um relogio") is False


def test_name_slot_ignores_other_conversation_on_same_phone():
    from app.sales.qualification_slots import (
        covered_qualification_dims,
        is_qualification_slot_answer,
        rehydrate_qualification_slots_from_turns,
    )

    foreign = [
        {
            "role": "assistant",
            "content": "Como posso te chamar?",
            "conversation_id": "old-thread",
            "metadata": {"safety_reason": "commerce_clarification"},
        },
        {"role": "user", "content": "João", "conversation_id": "old-thread"},
    ]
    assert (
        is_qualification_slot_answer(
            foreign, "Tironi", conversation_id="new-thread"
        )
        is False
    )
    assert (
        is_qualification_slot_answer(
            foreign, "Tironi", conversation_id="old-thread"
        )
        is True
    )
    blank = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={},
        references_previous_context=True,
        needs_clarification=True,
        confidence=0.9,
    )
    skipped = rehydrate_qualification_slots_from_turns(
        blank, foreign, conversation_id="new-thread"
    )
    assert CUSTOMER_NAME not in covered_qualification_dims(skipped)
    kept = rehydrate_qualification_slots_from_turns(
        blank, foreign, conversation_id="old-thread"
    )
    assert kept.preferences.recipient == "João"
    resumed = rehydrate_qualification_slots_from_turns(
        blank,
        foreign,
        conversation_id="new-thread",
        include_other_threads=True,
    )
    assert resumed.preferences.recipient == "João"
    assert is_qualification_slot_answer(
        foreign,
        "Tironi",
        conversation_id="new-thread",
        include_other_threads=True,
    )


def test_name_city_urgency_do_not_unlock_catalog_search():
    from app.sales.discovery import build_qualification_snapshot
    from app.sales.qualification_slots import (
        fulfillment_slots_ready,
        qualification_slots_sufficient,
    )
    from app.persona.persona_runtime import (
        PersonaRuntimeConfig,
        reset_persona_runtime,
        set_persona_runtime,
    )

    runtime = PersonaRuntimeConfig(
        loaded=True,
        enabled=True,
        require_qualification_before_catalog=True,
        qualification_prompts=["Qual faixa de investimento você tem em mente?"],
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="discover",
            subject={"product_type": "relógio"},
            preferences={
                "attributes": [
                    "qual:name:João",
                    "qual:city:Fortaleza",
                    "qual:urgency:can_wait",
                ],
                "recipient": "João",
            },
            references_previous_context=True,
            enough_information_to_search=False,
            ready_for_retrieval=False,
            needs_clarification=True,
            confidence=0.9,
        )
        covered = covered_qualification_dims(interpretation)
        assert qualification_slots_sufficient(interpretation, covered) is True
        assert fulfillment_slots_ready(interpretation, covered) is False
        snap = build_qualification_snapshot(
            interpretation,
            {"known_preferences": {}, "clarification_count": 0},
        )
        assert snap["ready"] is False
        assert snap["satisfied_by"] is None
        assert "customer_name" in snap["qualification_dims"]
    finally:
        reset_persona_runtime(token)


def test_city_uf_suffixes_are_plausible_slot_answers():
    from app.sales.qualification_slots import (
        _is_plausible_city,
        is_qualification_slot_answer,
    )

    turns = [
        {"role": "assistant", "content": "Para qual cidade e estado seria a entrega?"},
    ]
    assert _is_plausible_city("Florianópolis") is True
    assert _is_plausible_city("Conselheiro mairinck - pR") is True
    assert _is_plausible_city("Curitiba, PR") is True
    assert _is_plausible_city("Londrina/PR") is True
    assert is_qualification_slot_answer(turns, "Conselheiro mairinck - pR") is True
    assert _is_plausible_city("o orient open heart preto") is False
    assert is_qualification_slot_answer(turns, "o orient open heart preto") is False


def test_city_slot_answer_holds_retrieval_without_tray():
    from app.sales.qualification_slots import (
        SHIPPING_CITY,
        continue_commerce_from_qualification_answer,
        covered_qualification_dims,
    )

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={"product_type": "relógio"},
        preferences={
            "color": "dourado",
            "style": "social",
            "budget_max": 2500,
            "explicit_no_preferences": ["brand"],
        },
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    turns = [
        {"role": "assistant", "content": "Para qual cidade e estado seria a entrega?"},
    ]
    updated = continue_commerce_from_qualification_answer(
        interpretation,
        turns,
        "Conselheiro mairinck - pR",
    )
    assert SHIPPING_CITY in covered_qualification_dims(updated)
    assert updated._slot_answer_hold is True
    assert updated.ready_for_retrieval is False
    assert updated.enough_information_to_search is False

    import app.sales_agent as sales_agent
    from app.sales.intent_router import should_skip_catalog_fanout

    state = sales_agent._discovery_state(
        updated,
        turns,
        message_text="Conselheiro mairinck - pR",
    )
    assert state["force_retrieval"] is False
    assert state["slot_answer_hold"] is True
    assert should_skip_catalog_fanout(updated) is True


def test_name_without_brand_holds_and_named_brand_still_searches():
    from app.sales.qualification_slots import continue_commerce_from_qualification_answer

    leftover = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={
            "color": "dourado",
            "style": "social",
            "budget_max": 2500,
            "explicit_no_preferences": ["brand"],
        },
        references_previous_context=True,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    name_turns = [{"role": "assistant", "content": "Como posso te chamar?"}]
    named = continue_commerce_from_qualification_answer(
        leftover,
        name_turns,
        "Tironi",
    )
    assert named.preferences.recipient == "Tironi"
    assert named._slot_answer_hold is True
    assert named.ready_for_retrieval is False

    seiko = SalesInterpretation(
        domain="greeting",
        goal=None,
        subject={"brand": "Seiko", "product_type": "relógio"},
        preferences={},
        references_previous_context=True,
        needs_clarification=False,
        confidence=0.9,
    )
    joao = continue_commerce_from_qualification_answer(seiko, [], "Sou o João")
    assert joao.domain == "commerce"
    assert joao.preferences.recipient == "João"
    assert joao._slot_answer_hold is False


def test_brand_plus_budget_unlocks_without_persona_slots():
    from app.sales.discovery import build_qualification_snapshot
    from app.sales.qualification_slots import fulfillment_slots_ready
    from app.persona.persona_runtime import (
        PersonaRuntimeConfig,
        reset_persona_runtime,
        set_persona_runtime,
    )

    runtime = PersonaRuntimeConfig(
        loaded=True,
        enabled=True,
        require_qualification_before_catalog=True,
        qualification_prompts=["Como posso te chamar?"],
    )
    token = set_persona_runtime(runtime)
    try:
        interpretation = SalesInterpretation(
            domain="commerce",
            goal="recommend",
            subject={"brand": "Seiko", "product_type": "relógio"},
            preferences={"budget_max": 3500},
            references_previous_context=True,
            enough_information_to_search=True,
            ready_for_retrieval=True,
            needs_clarification=False,
            confidence=0.95,
        )
        assert fulfillment_slots_ready(interpretation) is True
        snap = build_qualification_snapshot(
            interpretation,
            {
                "known_preferences": {"brand": "Seiko", "budget": {"max": 3500}},
                "clarification_count": 0,
            },
        )
        assert snap["ready"] is True
        assert snap["satisfied_by"] == "brand+budget"
        assert snap["fulfillment_ready"] is True
    finally:
        reset_persona_runtime(token)
