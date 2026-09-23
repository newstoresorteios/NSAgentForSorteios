from copy import deepcopy

import pytest

from app.catalog.retrieval.offer_contract import seal_offer, responder_evidence
from app.catalog.retrieval.technical import technical_miss
from app.commerce.commerce_context import CommerceConversationState
from app.models import IncomingMessage
from app.verify.final_response import finalize_response
from tests.verify.test_final_quality_regressions import draft, interpretation, ASK


def finalize(result):
    return finalize_response(result, incoming=IncomingMessage(text=ASK),
                             interpretation=interpretation(), previous_state=CommerceConversationState())


@pytest.mark.parametrize("change", ["price", "available", "id", "description"])
def test_late_fact_changes_cannot_become_an_offer(change):
    result = seal_offer(draft())
    result.commercial_data["products"][0][change] = {
        "price": 0, "available": False, "id": "unapproved", "description": "quartzo mineral"
    }[change]
    final, state = finalize(result)
    assert final.commercial_data["products"] == []
    assert state.active_product is None
    assert state.last_presented_products == []
    assert "Modelo confirmado" not in final.reply_text


def test_rejected_product_cannot_return_through_text_only_rewrite():
    result = technical_miss(interpretation(), unknown=True)
    expected = result.reply_text
    result.reply_text = "Encontrei Modelo rejeitado sem preço. Quer comprar?"
    result.response_metadata["active_product"] = {"product_id": "rejected"}
    final, state = finalize(result)
    assert final.reply_text == expected
    assert state.active_product is None
    assert not final.handoff_required


def test_approved_offer_keeps_natural_wording_and_delivered_memory():
    result = seal_offer(draft())
    result.reply_text = "Encontrei o Modelo confirmado por R$ 2.200,00."
    final, state = finalize(result)
    assert "Modelo confirmado" in final.reply_text
    assert [p.product_id for p in state.last_presented_products] == ["ok"]


def test_rejected_full_details_stay_in_audit_only():
    result = seal_offer(draft())
    evidence = [{"product_id": "rejected", "stage": "live_detail", "product_name": "Rejected"},
                {"product_id": "ok", "stage": "candidate"},
                {"product_id": "ok", "stage": "live_detail", "status": "matched"}]
    result.response_metadata["technical_evidence"] = deepcopy(evidence)
    assert responder_evidence(result.response_metadata) == [evidence[-1]]
    assert result.response_metadata["technical_evidence"] == evidence


def test_contract_does_not_cross_into_another_turn():
    old = seal_offer(draft())
    new = draft()
    new.commercial_data["products"][0]["id"] = "next"
    final, state = finalize(new)
    assert [p.product_id for p in state.last_presented_products] == ["next"]
    assert old.response_metadata["offer_contract"]["products"][0]["id"] == "ok"


def test_audit_enrichment_does_not_invalidate_live_offer():
    result = seal_offer(draft())
    result.commercial_data['products'][0].update(_grounded=True,tenant_id='newstore')
    final,state = finalize(result)
    assert [p.product_id for p in state.last_presented_products] == ['ok']


@pytest.mark.parametrize('price',[None,0,-1,'NaN','Infinity'])
def test_generic_offer_admission_requires_valid_price(price):
    from app.catalog.retrieval.offer_contract import validate_recommendation
    result = draft()
    result.commercial_data['products'][0]['price'] = price
    fixed = validate_recommendation(result, interpretation(), force=True)
    assert not fixed.commercial_data['products']
    assert fixed.response_metadata['rejected_candidates']
    assert fixed.response_metadata['missing_evidence']


def test_generic_inspect_preserves_negative_facts():
    from app.catalog.retrieval.offer_contract import validate_recommendation
    result=draft()
    result.commercial_data['products'][0]['price']=None
    interp=interpretation();interp.goal='inspect'
    assert validate_recommendation(result,interp,force=True) is result
