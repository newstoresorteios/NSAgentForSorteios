from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.specs.preference_normalize import normalize_sales_interpretation
from app.commerce.commerce_context import CommerceConversationState
from app.commerce.commerce_router import _product_result
from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.answer_council import apply_turn_contract_for_search
from app.sales.contextual_questions import normalize_followup
from app.verify.final_response import finalize_response


MK2 = {
    "id": "14738",
    "brand": "Baltic",
    "model": "Aquascaphe MK2",
    "name": "Relógio Baltic Aquascaphe MK2 Automático Cinza 37mm",
    "description": "Relógio com pulseiras disponíveis em borracha ou aço inoxidável",
    "variants": [
        {"id": "1714", "name": "Borracha"},
        {"id": "1716", "name": "Aço inoxidável"},
    ],
    "price": 9299.99,
    "available": True,
    "_revalidated": True,
    "_factual_source": "tray_live",
}

MR01_LEATHER = {
    "id": "11515",
    "brand": "Baltic",
    "model": "MR01",
    "name": "Relógio Baltic MR01 Automático Salmão Marrom",
    "description": "Caixa em aço inoxidável e pulseira de couro marrom",
    "price": 7699.99,
    "available": True,
    "_revalidated": True,
    "_factual_source": "tray_live",
}


def _interpretation(*, goal: str, model: str | None, material: str | None = None,
                    references_previous_context: bool = False) -> SalesInterpretation:
    return SalesInterpretation(
        domain="commerce",
        goal=goal,
        confidence=.99,
        needs_clarification=False,
        references_previous_context=references_previous_context,
        subject={"brand": "Baltic", "model": model, "product_type": "relógio"},
        preferences={
            "material": material,
            "attributes": ["case_size:37-37mm"],
        },
    )


def test_full_baltic_mk2_steel_followup_survives_new_conversation_id():
    initial = _interpretation(goal="inspect", model="MK2")
    first_result = AgentResult(
        reply_text=(
            "Encontrei o Baltic Aquascaphe MK2 Automático Cinza 37mm. "
            "Você quer a versão em borracha ou em aço?"
        ),
        intent="commerce",
        commercial_data={"products": [MK2]},
        response_metadata={
            "domain": "commerce",
            "interpretation": initial.model_dump(mode="json"),
            "presented_products": True,
            "product_resolution_state": "found_available",
        },
    )
    _, state = finalize_response(
        first_result,
        incoming=IncomingMessage(text="Quero saber o preço de Baltic MK2 37mm"),
        interpretation=initial,
        previous_state=CommerceConversationState(),
    )
    assert [item.product_id for item in state.last_presented_products] == ["14738"]

    steel_answer = _interpretation(
        goal="buy",
        model="Aquascaphe MK2",
        material="aço inoxidável",
        references_previous_context=True,
    )
    steel_answer.reference_type = "last_presented_product"
    steel_answer.answer_strategy = "acknowledge"
    refined, state = normalize_followup("Aço", steel_answer, state)
    normalize_sales_interpretation(refined, message_text="Aço")
    refined = apply_turn_contract_for_search(
        refined,
        message_text="Aço",
        commerce_state=state,
    )
    assert refined.goal == "find"
    assert "mk2" in str(refined.subject.model).casefold()
    assert "required_strap_material:aço" in refined.preferences.attributes
    assert [item["id"] for item in hard_filter_products(
        [MR01_LEATHER, MK2], refined, mode="recommendation", message_text="Aço"
    )] == ["14738"]

    # Brevo may create another conversation ID. The durable shortlist must
    # still bind a same-brand variant refinement to the MK2.
    new_thread = _interpretation(goal="find", model=None, material="aço")
    normalize_sales_interpretation(
        new_thread,
        message_text="Quero o Baltic com pulseira de aço",
    )
    rebound = apply_turn_contract_for_search(
        new_thread,
        message_text="Quero o Baltic com pulseira de aço",
        commerce_state=state,
    )
    candidates = hard_filter_products(
        [MR01_LEATHER, MK2],
        rebound,
        mode="recommendation",
        message_text="Quero o Baltic com pulseira de aço",
    )
    reply = _product_result("product_search", candidates).reply_text

    assert rebound.subject.model and "mk2" in rebound.subject.model.casefold()
    assert [item["id"] for item in candidates] == ["14738"]
    assert "Aquascaphe MK2" in reply
    assert "MR01" not in reply
    assert "1. Relógio" not in reply
