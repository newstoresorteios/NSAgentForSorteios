import pytest

from app.models import SalesInterpretation
from app.sales.qualification_slots import (
    SHIPPING_CITY, apply_qualification_slot_answer, classify_qualification_question,
    continue_commerce_from_qualification_answer, covered_qualification_dims,
    merge_persisted_qualification_slots, rehydrate_qualification_slots_from_turns,
)


def interpretation(**values):
    return SalesInterpretation(domain="commerce", references_previous_context=True,
                               needs_clarification=False, confidence=0.95, **values)


@pytest.mark.parametrize("answer", [
    "Tenho um casamento para o próximo final de semana", "Está difícil entender?",
    "PQP", "Já falei", "Preciso receber amanhã", "Não", "Bom dia",
])
def test_pending_city_does_not_turn_feedback_or_deadline_into_location(answer):
    turns = [{"role": "assistant", "content": "Para qual cidade seria a entrega?"}]
    result = rehydrate_qualification_slots_from_turns(
        interpretation(), turns, message_text=answer,
    )
    assert SHIPPING_CITY not in covered_qualification_dims(result)
    legacy = {"qualification_slots": {"shipping_city": answer}, "attributes": [f"qual:city:{answer}"]}
    cleaned = merge_persisted_qualification_slots(legacy, legacy)
    assert "shipping_city" not in cleaned["qualification_slots"]
    assert not cleaned["attributes"]


@pytest.mark.parametrize("city", ["Rio de Janeiro", "Conselheiro Mairinck - PR", "Curitiba, PR", "Londrina/PR", "São José dos Campos"])
def test_bare_city_answers_remain_supported(city):
    result = apply_qualification_slot_answer(interpretation(), SHIPPING_CITY, city)
    assert f"qual:city:{city}" in result.preferences.attributes


def test_semantic_feedback_does_not_overwrite_city_or_hold_catalog_search():
    parsed = interpretation(conversation_feedback="frustrated")
    turns = [
        {"role": "assistant", "content": "Para qual cidade seria a entrega?"},
        {"role": "user", "content": "Rio de Janeiro"},
        {"role": "assistant", "content": "Para qual cidade seria a entrega?"},
    ]
    result = rehydrate_qualification_slots_from_turns(parsed, turns, message_text="Absurdo")
    assert result.preferences.attributes == ["qual:city:Rio de Janeiro"]
    continued = continue_commerce_from_qualification_answer(result, turns, "Absurdo")
    assert not getattr(continued, "_slot_answer_hold", False)


def test_delivery_statement_does_not_create_city_question():
    assert classify_qualification_question("O prazo de entrega é de 30 dias.") is None
    assert classify_qualification_question("Qual cidade?") == SHIPPING_CITY
    assert classify_qualification_question("Qual a cidade para entrega?") == SHIPPING_CITY
