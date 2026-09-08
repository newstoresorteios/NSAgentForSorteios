from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage, SalesInterpretation
from app.sales.answer_council import (
    _honest_constraint_reply, apply_answer_council_with_retry,
    apply_turn_contract_for_search,
)
from app.sales.turn_contract import TurnContract


def product(**changes):
    return {"id": "1", "name": "Citizen Promaster", "brand": "Citizen",
            "price": 2200, "available": True, **changes}


def draft(products=None, **changes):
    return AgentResult(
        reply_text="Para trabalho eu separaria estas opções.", intent="commerce",
        commercial_data={"products": products if products is not None else [product()]},
        **changes,
    )


def contract(**changes):
    return TurnContract(budget_max=2500, must_not_claim_stale_occasion=True, **changes)


@pytest.mark.asyncio
async def test_council_repairs_prose_before_discarding_valid_catalog(monkeypatch):
    monkeypatch.setattr("app.sales.answer_council.get_settings", lambda: SimpleNamespace(
        agent_answer_council_enabled=True, agent_answer_council_max_restarts=1,
    ))
    monkeypatch.setattr("app.sales.answer_council.build_turn_contract", lambda **kwargs: contract())
    async def no_lookup(*args, **kwargs):
        pytest.fail("Prose repair must not discard valid candidates and query again")
    monkeypatch.setattr("app.sales.product_lookup.execute_compiled_product_retrieval", no_lookup)
    result, decision, _ = await apply_answer_council_with_retry(
        draft(response_metadata={"request_id": "synthetic-test"}),
        incoming=IncomingMessage(channel="whatsapp", text="outras marcas nessa faixa"),
        interpretation=None,
    )
    assert "Citizen Promaster" in result.reply_text
    assert "trabalho" not in result.reply_text
    assert decision.approved is True
    assert decision.restart is False
    assert decision.issues == []
    assert decision.checker_a["pass_check"] is True
    assert decision.checker_b["pass_check"] is True
    assert result.response_metadata["answer_council_repaired_issues"] == ["stale_occasion_claimed"]
    assert result.response_metadata["request_id"] == "synthetic-test"


@pytest.mark.parametrize("bad", [
    product(price=3000), product(price=None), product(price="NaN"),
    product(price="Infinity"), product(price=0), product(available=False), product(id=None),
])
def test_recomposition_never_promotes_invalid_product_data(bad):
    result = _honest_constraint_reply(draft([bad]), contract(), None)
    assert not result.commercial_data["products"]
    assert result.safety_reason == "answer_council_blocked"
    assert "Não consegui confirmar" in result.reply_text
    assert "Não encontrei" not in result.reply_text


def test_mixed_brand_list_cannot_pass_recomposition_using_one_correct_sibling():
    result = _honest_constraint_reply(
        draft([product(), product(id="2", brand="Seiko", name="Seiko 5")]),
        contract(brand="Citizen"), None,
    )
    assert not result.commercial_data["products"]


@pytest.mark.parametrize("metadata,safety", [
    ({"guided_near_match": True}, None),
    ({"factual_validation": {"violations": [{"code": "unsupported_price"}]}}, None),
    ({}, "factual_validation_failed"),
])
def test_factual_failure_or_near_match_is_not_relabelled_as_valid(metadata, safety):
    result = _honest_constraint_reply(
        draft(response_metadata=metadata, safety_reason=safety), contract(), None,
    )
    assert not result.commercial_data["products"]
    assert not result.response_metadata.get("answer_council_recomposed")


def test_blocked_copy_clears_previous_audio_and_recomposition_marker():
    result = _honest_constraint_reply(
        draft([product(price=3000)], reply_modality="audio", reply_audio_bytes=b"old",
              reply_audio_url="https://example.invalid/old.ogg",
              reply_audio_mime_type="audio/ogg",
              response_metadata={"answer_council_recomposed": True}),
        contract(), None,
    )
    assert result.reply_modality == "text"
    assert result.reply_audio_bytes is None
    assert result.reply_audio_url is None
    assert result.reply_audio_mime_type is None
    assert not result.response_metadata.get("answer_council_recomposed")


@pytest.mark.parametrize("attributes", [[], ["somente:Citizen"]])
def test_other_brands_invalidates_all_cached_brand_locks(attributes):
    from app.catalog.retrieval.hard_filter import hard_filter_products
    from app.catalog.specs.catalog_specs import apply_brand_unlock_to_interpretation
    from app.llm.turn_understanding import sales_to_turn_understanding

    interp = SalesInterpretation(
        domain="commerce", goal="recommend", information_needed=["catalog"],
        subject={"product_type": "relógio", "brand": "Citizen", "model": "Promaster"},
        preferences={"budget_max": 2500, "attributes": attributes},
        ready_for_retrieval=True, enough_information_to_search=True,
        references_previous_context=True, needs_clarification=False, confidence=0.99,
    )
    interp._turn_understanding = sales_to_turn_understanding(interp, message_text="Citizen")
    assert interp._turn_understanding.hard_constraints.brand == "Citizen"
    interp._turn_contract_bound = True
    apply_brand_unlock_to_interpretation(interp, message_text="outras marcas nessa faixa")
    assert interp._turn_understanding is None
    assert interp._turn_contract_bound is False
    bound = apply_turn_contract_for_search(interp, message_text="outras marcas nessa faixa")
    found = hard_filter_products(
        [product(id="2", brand="Orient", name="Orient"), product(id="3", price=3000)],
        bound, mode="recommendation",
    )
    assert [row["id"] for row in found] == ["2"]
    assert bound.preferences.budget_max == 2500
