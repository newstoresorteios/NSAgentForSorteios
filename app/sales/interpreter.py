"""Turn interpreter facade. Planner lives here; LLM interpret stays on sales_agent."""

from __future__ import annotations

from typing import Any

from app.models import SalesInterpretation


def interpretation_to_plan(
    interpretation: SalesInterpretation,
    text: str | None = None,
) -> dict[str, Any]:
    subject = interpretation.subject.model_dump()
    preferences = interpretation.preferences.model_dump()
    if subject.get("reference"):
        query_parts = [str(subject["reference"])]
    elif subject.get("ean"):
        query_parts = [str(subject["ean"])]
    elif subject.get("brand") or subject.get("model"):
        query_parts = [str(value) for value in (subject.get("brand"), subject.get("model")) if value]
    elif subject.get("product_type"):
        query_parts = [str(subject["product_type"])]
    else:
        query_parts = []
    query = " ".join(query_parts).strip()

    information_needed = set(interpretation.information_needed)
    inspect_intent = (
        "inventory" if "inventory" in information_needed
        else "coupon" if "coupons" in information_needed
        else "price" if information_needed.intersection({"price", "payment"})
        else "product_search"
    )
    goal_to_intent = {
        "discover": "clarification",
        "find": "product_search",
        "recommend": "recommendation",
        "compare": "product_comparison",
        "inspect": inspect_intent,
        "buy": "clarification",
        "after_sales": "clarification",
    }
    retrieval_signal = any((
        interpretation.enough_information_to_search,
        interpretation.ready_for_retrieval,
        interpretation.stop_clarification,
    ))
    strategy = interpretation.resolved_answer_strategy()
    talk_only = strategy in {"acknowledge", "clarify", "handoff", "refuse"}
    # Inspect never becomes compiled product_search unless the turn asked to search.
    # Fallback without TurnUnderstanding used to map goal=inspect + catalog → product_search.
    inspect_talk = interpretation.goal == "inspect" and strategy != "search_catalog"
    if interpretation.purchase_action == "create_cart":
        intent = "purchase_intent"
    elif talk_only:
        intent = "clarification"
    elif inspect_talk:
        intent = inspect_intent if inspect_intent != "product_search" else "clarification"
    elif retrieval_signal and interpretation.goal == "recommend":
        intent = "recommendation"
    elif retrieval_signal and interpretation.goal == "find":
        intent = "product_search"
    else:
        intent = "clarification" if interpretation.needs_clarification else goal_to_intent.get(
            interpretation.goal or "discover",
            "clarification",
        )
    filters = {
        key: value
        for key, value in {
            "brand": subject.get("brand"),
            "model": subject.get("model"),
            "reference": subject.get("reference"),
            "ean": subject.get("ean"),
            "budget_min": preferences.get("budget_min"),
            "budget_max": preferences.get("budget_max"),
            "attributes": preferences.get("attributes"),
            "color": preferences.get("color"),
            "style": preferences.get("style"),
            "material": preferences.get("material"),
        }.items()
        if value not in (None, [], "")
    }
    return {
        "domain": interpretation.domain,
        "intent": intent,
        "goal": interpretation.goal,
        "subject": {**subject, "query": query},
        "constraints": preferences,
        "query": query,
        "filters": filters,
        "budget_max": preferences.get("budget_max"),
        "product_type": subject.get("product_type"),
        "needs_clarification": interpretation.needs_clarification,
        "clarification_question": interpretation.clarification_question,
        "information_needed": interpretation.information_needed,
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "purchase_action": interpretation.purchase_action,
        "quantity": interpretation.quantity,
        "purchase_items": [
            item.model_dump(mode="json")
            for item in interpretation.purchase_items
        ],
        "image_request": interpretation.image_request,
        "product_action": interpretation.product_action,
        "payment_action": interpretation.payment_action,
        "payment_method_preference": interpretation.payment_method_preference,
        "payment_option_id": interpretation.payment_option_id,
        "checkout_channel_preference": interpretation.checkout_channel_preference,
        "shipping_action": interpretation.shipping_action,
        "shipping_zipcode": interpretation.shipping_zipcode,
        "shipping_selection_id": interpretation.shipping_selection_id,
        "shipping_selection_position": interpretation.shipping_selection_position,
        "checkout_action": interpretation.checkout_action,
        "checkout_data": (
            interpretation.checkout_data.model_dump(mode="json", exclude_none=True)
            if interpretation.checkout_data else None
        ),
        "order_action": interpretation.order_action,
        "order_id": interpretation.order_id,
        "confirmation": interpretation.confirmation,
        "installment_count": interpretation.installment_count,
        "_source": interpretation._source,
    }


async def interpret_message(*args: Any, **kwargs: Any):
    """Compatibility wrapper. Body stays on sales_agent so monkeypatches keep working."""
    from app.sales_agent import interpret_message as _interpret

    return await _interpret(*args, **kwargs)
