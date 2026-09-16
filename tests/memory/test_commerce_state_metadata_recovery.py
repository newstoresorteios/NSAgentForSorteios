from app.core.db import (
    _commerce_state_from_provider_response,
    _merge_durable_commerce_state,
)
from app.commerce.commerce_context import (
    CommerceConversationState,
    evolve_commerce_state,
)
from app.models import AgentResult


def test_commerce_state_falls_back_to_persisted_agent_metadata():
    state = {
        "dialogue_phase": "shortlist",
        "forget_shortlist": False,
        "last_presented_products": [
            {"position": 2, "product_id": "12343", "name": "Hamilton Murph"}
        ],
    }

    recovered = _commerce_state_from_provider_response(
        {"_agent_metadata": {"commerce_state": state}}
    )

    assert recovered == state


def test_agent_context_remains_primary_when_both_formats_exist():
    recovered = _commerce_state_from_provider_response(
        {
            "_agent_context": {"commerce_state": {"dialogue_phase": "buy"}},
            "_agent_metadata": {
                "commerce_state": {"dialogue_phase": "shortlist"}
            },
        }
    )

    assert recovered["dialogue_phase"] == "buy"


def test_stale_durable_tombstone_does_not_erase_delivered_shortlist():
    delivered = {
        "dialogue_phase": "shortlist",
        "forget_shortlist": False,
        "last_presented_products": [
            {"position": 2, "product_id": "12343", "name": "Hamilton Murph"}
        ],
    }
    stale_durable = {
        "dialogue_phase": "discovery",
        "forget_shortlist": True,
        "last_presented_products": [],
        "active_product": None,
    }

    merged = _merge_durable_commerce_state(delivered, stale_durable)

    assert merged["forget_shortlist"] is False
    assert merged["dialogue_phase"] == "shortlist"
    assert merged["last_presented_products"][0]["product_id"] == "12343"


def test_confirmed_active_product_revives_sale_after_old_tombstone():
    previous = CommerceConversationState(
        active_domain="commerce",
        forget_shortlist=True,
    )
    result = AgentResult(
        reply_text="Produto confirmado.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "active_product": {
                "product_id": "12343",
                "name": "Hamilton Murph H70405130",
                "reference": "H70405130",
            },
        },
    )

    updated = evolve_commerce_state(previous, result)

    assert updated.active_product is not None
    assert updated.active_product.product_id == "12343"
    assert updated.forget_shortlist is False
