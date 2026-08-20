from app.commerce_context import CommerceConversationState
from app.sales.policies.action_authority import purchase_product_required_result
from app.sales.policies.confirmation import confirmation_text_kind
from app.sales.workflows.catalog_ranking import rank_candidates, score_candidate
from app.sales_agent import (
    _confirmation_text_kind,
    _purchase_product_required_result,
    rank_candidates as sales_rank_candidates,
    score_candidate as sales_score_candidate,
)


def test_catalog_ranking_prefers_exact_model():
    plan = {
        "subject": {"brand": "Tissot", "model": "Seastar"},
        "constraints": {},
    }
    ranked = rank_candidates(
        [
            {"name": "Relógio Tissot PRC 200", "brand": "Tissot", "current_price": "1000"},
            {
                "name": "Relógio Tissot Seastar 1000",
                "brand": "Tissot",
                "model": "Seastar",
                "current_price": "1990",
            },
        ],
        plan,
    )
    assert ranked[0]["model"] == "Seastar"
    assert score_candidate(ranked[0], plan) > 0
    assert sales_rank_candidates is rank_candidates
    assert sales_score_candidate is score_candidate


def test_confirmation_wrappers_stay_compatible():
    state = CommerceConversationState(
        pending_action="awaiting_order_confirmation",
        order_confirmation_status="pending",
        order_review_version="rv1",
    )
    assert confirmation_text_kind(state, "sim") == "confirm"
    assert confirmation_text_kind(state, "não") == "reject"
    assert confirmation_text_kind(state, "quero pix") == "change"
    assert _confirmation_text_kind is confirmation_text_kind


def test_action_authority_wrapper_compatible():
    state = CommerceConversationState()
    result = purchase_product_required_result(state)
    assert result.safety_reason == "no_cart_no_product"
    assert _purchase_product_required_result is purchase_product_required_result
    assert "carrinho" not in result.reply_text.lower()


def test_informational_payment_policy_without_cart():
    from app.sales.policies.action_authority import (
        informational_payment_policy_result,
        is_informational_payment_query,
    )
    from app.models import SalesInterpretation

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="inspect",
        subject={"product_type": "produto"},
        preferences={},
        information_needed=["payment"],
        references_previous_context=True,
        needs_clarification=False,
        payment_action="payment_options",
        payment_method_preference="pix",
        payment_request_kind="informational",
        confidence=0.9,
    )
    assert is_informational_payment_query(
        interpretation,
        purchase_action=None,
    )
    result = informational_payment_policy_result(
        CommerceConversationState(
            last_presented_products=[
                {"position": 1, "product_id": "1", "name": "Seiko"},
            ]
        ),
        payment_method_preference="pix",
    )
    assert result.safety_reason == "informational_payment_no_cart"
    assert "15%" in result.reply_text
    assert result.commercial_data["cart"]["status"] == (
        "not_required_for_informational_payment"
    )
