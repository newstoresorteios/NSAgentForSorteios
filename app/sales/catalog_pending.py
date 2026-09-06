"""Pending-action confirmation and purchase-item SKU resolution.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


@dataclass
class CatalogPendingResolution:
    interpretation: SalesInterpretation | None
    plan: dict[str, Any]
    purchase_action: str | None
    resolved_product: Any
    resolved_by: str
    purchase_requests: list[Any] = field(default_factory=list)
    unresolved_purchase_items: int = 0
    unresolved_candidates: list[dict[str, Any]] = field(default_factory=list)
    pending_link_requested: bool = False
    pending_action_used: bool = False
    early_result: AgentResult | None = None


async def apply_catalog_pending(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    purchase_action: str | None,
    resolved_product: Any,
    resolved_by: str,
) -> CatalogPendingResolution:
    sales = _sales()
    purchase_requests: list[Any] = []
    unresolved_purchase_items = 0
    unresolved_candidates: list[dict[str, Any]] = []
    pending_link_requested = bool(
        interpretation
        and interpretation.product_action == "get_product_link"
    )
    pending_action_used = False
    if (
        interpretation is not None
        and state.pending_action == "show_nearby_line"
        and (
            interpretation.confirmation == "confirm"
            or sales.is_short_affirmation(message.text)
        )
    ):
        from app.catalog.vision.image_product_id import (
            ImageProductIdentification,
            soft_line_interpretation_from_identification,
        )

        prefs = state.active_preferences if isinstance(state.active_preferences, dict) else {}
        identify_payload = prefs.get("image_identify")
        soft_interp = None
        if isinstance(identify_payload, dict):
            try:
                soft_interp = soft_line_interpretation_from_identification(
                    ImageProductIdentification.model_validate(identify_payload)
                )
            except (TypeError, ValueError):
                soft_interp = None
        if soft_interp is None:
            brand = prefs.get("nearby_line_brand") or (
                state.active_product.brand if state.active_product else None
            )
            model = prefs.get("nearby_line_model")
            if brand or model:
                soft_interp = SalesInterpretation(
                    domain="commerce",
                    goal="recommend",
                    subject={
                        "product_type": "relógio",
                        "brand": brand,
                        "model": model,
                    },
                    preferences={
                        "color": prefs.get("nearby_line_color"),
                    },
                    information_needed=["catalog"],
                    references_previous_context=True,
                    enough_information_to_search=True,
                    ready_for_retrieval=True,
                    stop_clarification=True,
                    needs_clarification=False,
                    clarification_question=None,
                    confidence=0.8,
                    active_topic="nearby_line_options",
                    purchase_stage="discovery",
                    confirmation="confirm",
                )
                soft_interp._force_recommendation_mode = True
                soft_interp._source = "nearby_line_resume"
        if soft_interp is not None:
            print("[sales.pending_action]", {
                "action": "show_nearby_line",
                "confirmed": True,
                "brand": soft_interp.subject.brand,
                "model": soft_interp.subject.model,
            })
            tray_result = await sales._execute_compiled_product_retrieval(
                soft_interp,
                message_text=message.text,
                commerce_state=state,
            )
            interpretation._clear_pending_action = True
            pending_action_used = True
            if tray_result is not None:
                return CatalogPendingResolution(
                    interpretation=interpretation,
                    plan=plan,
                    purchase_action=purchase_action,
                    resolved_product=resolved_product,
                    resolved_by=resolved_by,
                    pending_action_used=True,
                    early_result=sales._mark_sales_result(
                        tray_result,
                        interpretation=soft_interp,
                        goal="recommend",
                        response_source=tray_result.response_metadata.get(
                            "response_source",
                            "nearby_line_resume",
                        ),
                        used_openai_responder=False,
                        used_tray=True,
                        fallback_reason=tray_result.safety_reason,
                    ),
                )
            return CatalogPendingResolution(
                interpretation=interpretation,
                plan=plan,
                purchase_action=purchase_action,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                pending_action_used=True,
                early_result=sales._mark_sales_result(
                    AgentResult(
                        reply_text=(
                            "Ainda não achei opções dessa linha no catálogo agora. "
                            "Se tiver a referência do modelo, me manda que eu confiro."
                        ),
                        intent="commerce",
                        handoff_required=False,
                        safety_reason="product_not_found",
                        response_metadata={
                            "domain": "commerce",
                            "clear_pending_action": True,
                        },
                    ),
                    interpretation=soft_interp,
                    goal="recommend",
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=True,
                    fallback_reason="nearby_line_empty",
                ),
            )
    if (
        interpretation is not None
        and state.pending_action
        and state.pending_action not in {"show_nearby_line", "awaiting_payment"}
        and interpretation.confirmation == "confirm"
        and interpretation.goal in {"discover", "find", "recommend", "compare"}
    ):
        # A new explicit discovery request is not confirmation of an old checkout step.
        interpretation._clear_pending_action = True
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "confirm"
        and purchase_action not in {"set_cart_item_quantity", "remove_cart_item"}
        and interpretation.payment_action is None
        and interpretation.checkout_data is None
        and interpretation.shipping_action is None
        and interpretation.goal not in {"discover", "find", "recommend", "compare"}
        and not (
            purchase_action == "create_cart"
            and interpretation.reference_type in {"list_position", "explicit_product"}
        )
    ):
        pending_references = sales._pending_product_references(state)
        pending_action = state.pending_action
        from app.commerce.checkout_service import cart_pay_link_copy, visible_cart_url

        live_url = visible_cart_url(state)
        if live_url and pending_action != "awaiting_shipping_zipcode":
            interpretation._clear_pending_action = True
            pay_result = AgentResult(
                reply_text=cart_pay_link_copy(cart_url=live_url),
                intent="commerce",
                handoff_required=False,
                commercial_data={
                    "cart": {
                        "status": "cart_created",
                        "cart_url": live_url,
                    }
                },
                response_metadata={
                    "domain": "commerce",
                    "purchase_stage": "cart_created",
                    "clear_pending_action": True,
                    "used_tray": False,
                },
            )
            return CatalogPendingResolution(
                interpretation=interpretation,
                plan=plan,
                purchase_action=purchase_action,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                purchase_requests=purchase_requests,
                unresolved_purchase_items=unresolved_purchase_items,
                unresolved_candidates=unresolved_candidates,
                pending_link_requested=pending_link_requested,
                pending_action_used=True,
                early_result=sales._mark_sales_result(
                    pay_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                ),
            )
        interpretation._clear_pending_action = True
        if pending_action in {"create_cart", "confirm_purchase"}:
            purchase_action = "create_cart"
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
            elif pending_references:
                purchase_requests.extend(
                    sales.CartItemRequest(
                        product_reference=item,
                        quantity=interpretation.quantity or 1,
                        resolved_from="pending_action",
                        variant_preferences=interpretation.preferences.model_dump(
                            mode="json",
                            exclude_none=True,
                        ),
                    )
                    for item in pending_references
                )
        elif pending_action == "show_images":
            interpretation = interpretation.model_copy(update={"image_request": True})
            interpretation._clear_pending_action = True
            plan = sales.interpretation_to_plan(interpretation, message.text)
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "show_payment_options":
            interpretation = interpretation.model_copy(
                update={
                    "payment_action": "payment_options",
                    "purchase_action": (
                        "create_cart"
                        if not state.cart_session_id and len(pending_references) == 1
                        else interpretation.purchase_action
                    ),
                }
            )
            interpretation._clear_pending_action = True
            plan = sales.interpretation_to_plan(interpretation, message.text)
            purchase_action = interpretation.purchase_action
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "send_product_link":
            pending_link_requested = True
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "choose_checkout_channel":
            interpretation._clear_pending_action = bool(
                interpretation.checkout_channel_preference
            )
            pending_action_used = bool(
                interpretation.checkout_channel_preference
            )
        elif pending_action == "awaiting_shipping_zipcode":
            interpretation._clear_pending_action = False
            requirement_result = AgentResult(
                reply_text="Ainda existe um requisito factual de entrega pendente.",
                intent="commerce",
                handoff_required=False,
                safety_reason="checkout_requirements_missing",
                commercial_data={
                    "checkout_ready_for_payment": False,
                    "missing_checkout_requirements": ["shipping_zipcode"],
                    "checkout_blockers": ["shipping_zipcode_missing"],
                },
                response_metadata={
                    "domain": "commerce",
                    "purchase_stage": "shipping",
                    "pending_action": "awaiting_shipping_zipcode",
                    "pending_action_product_ids": [],
                    "used_tray": False,
                },
            )
            return CatalogPendingResolution(
                interpretation=interpretation,
                plan=plan,
                purchase_action=purchase_action,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                pending_link_requested=pending_link_requested,
                early_result=await sales._respond_to_commerce_service(
                    message=message,
                    plan=plan,
                    result=requirement_result,
                    interpretation=interpretation,
                ),
            )
        pending_action_used = bool(
            pending_action_used
            or pending_references
            or (
                pending_action == "show_payment_options"
                and state.cart_session_id
            )
        )
        print("[sales.pending_action]", {
            "action": pending_action,
            "has_product": bool(pending_references),
            "confirmation": interpretation.confirmation,
            "executed": bool(
                resolved_product
                or purchase_requests
                or pending_action == "show_payment_options"
                or (
                    pending_action == "choose_checkout_channel"
                    and interpretation.checkout_channel_preference
                )
            ),
        })
    if interpretation is not None:
        print("[sales.state.application]", {
            "had_pending_action": bool(state.pending_action),
            "pending_action_used": pending_action_used,
            "pending_action_cleared": interpretation._clear_pending_action,
            "had_active_product": state.active_product is not None,
            "active_product_referenced": bool(
                interpretation.reference_type == "current_product"
                and resolved_product is not None
            ),
        })
    if interpretation is not None and interpretation.purchase_items:
        for item in interpretation.purchase_items:
            sales.log_purchase_progress("reference_resolution", "start")
            item_ref, item_resolved_by = sales.resolve_purchase_item_reference(item, state)
            sales.log_purchase_progress(
                "reference_resolution",
                "success" if item_ref is not None else "blocked",
                None if item_ref is not None else "purchase_item_not_resolved",
            )
            if (
                item_ref is None
                and item.reference_type == "explicit_product"
                and item.explicit_product_name
            ):
                sales.log_purchase_progress("product_resolution", "start")
                item_subject = interpretation.subject.model_copy(update={
                    "model": item.explicit_product_name,
                    "reference": None,
                    "ean": None,
                })
                item_interpretation = interpretation.model_copy(
                    deep=True,
                    update={
                        "goal": "find",
                        "subject": item_subject,
                        "purchase_action": None,
                        "purchase_items": [],
                        "quantity": None,
                        "needs_clarification": False,
                        "ready_for_retrieval": True,
                    },
                )
                lookup = await sales._execute_compiled_product_retrieval(
                    item_interpretation,
                    message_text=message.text,
                    commerce_state=state,
                )
                candidates = (
                    (lookup.commercial_data or {}).get("products")
                    if lookup is not None
                    else None
                )
                candidates = candidates if isinstance(candidates, list) else []
                if len(candidates) == 1 and isinstance(candidates[0], dict):
                    item_ref = sales.product_reference_from_product(candidates[0])
                    item_resolved_by = "explicit_product"
                    sales.log_purchase_progress("product_resolution", "success")
                elif candidates:
                    unresolved_candidates = [
                        candidate
                        for candidate in candidates[:3]
                        if isinstance(candidate, dict)
                    ]
                    sales.log_purchase_progress(
                        "product_resolution",
                        "blocked",
                        "ambiguous_purchase_item",
                    )
                else:
                    sales.log_purchase_progress(
                        "product_resolution",
                        (
                            "failed"
                            if lookup is not None and lookup.safety_reason
                            else "blocked"
                        ),
                        (
                            lookup.safety_reason
                            if lookup is not None and lookup.safety_reason
                            else "product_not_found"
                        ),
                    )
            if item_ref is None:
                unresolved_purchase_items += 1
                continue
            purchase_requests.append(sales.CartItemRequest(
                product_reference=item_ref,
                quantity=item.quantity,
                position=item.reference_position,
                resolved_from=item_resolved_by,
                variant_preferences=interpretation.preferences.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            ))
        print("[sales.cart.items]", {
            "requested_count": len(interpretation.purchase_items),
            "resolved_count": len(purchase_requests),
        })
    if (
        interpretation is not None
        and (
            purchase_action == "create_cart"
            or interpretation.product_action == "get_product_link"
        )
        and not purchase_requests
        and resolved_product is None
        and any((
            interpretation.subject.reference,
            interpretation.subject.ean,
            interpretation.subject.model,
        ))
    ):
        sales.log_purchase_progress("product_resolution", "start")
        lookup = await sales._execute_compiled_product_retrieval(
            interpretation,
            message_text=message.text,
            commerce_state=state,
        )
        lookup_products = (
            (lookup.commercial_data or {}).get("products")
            if lookup is not None
            else None
        )
        lookup_products = lookup_products if isinstance(lookup_products, list) else []
        if len(lookup_products) == 1 and isinstance(lookup_products[0], dict):
            resolved_product = sales.product_reference_from_product(lookup_products[0])
            resolved_by = "product_id"
            sales.log_purchase_progress("product_resolution", "success")
        elif lookup_products:
            unresolved_purchase_items = 1
            unresolved_candidates = [
                candidate
                for candidate in lookup_products[:3]
                if isinstance(candidate, dict)
            ]
            sales.log_purchase_progress(
                "product_resolution",
                "blocked",
                "ambiguous_purchase_item",
            )
        elif lookup is not None:
            sales.log_purchase_progress(
                "product_resolution",
                "failed" if lookup.safety_reason else "blocked",
                lookup.safety_reason or "product_not_found",
            )
            return CatalogPendingResolution(
                interpretation=interpretation,
                plan=plan,
                purchase_action=purchase_action,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                purchase_requests=purchase_requests,
                unresolved_purchase_items=unresolved_purchase_items,
                unresolved_candidates=unresolved_candidates,
                pending_link_requested=pending_link_requested,
                pending_action_used=pending_action_used,
                early_result=sales._mark_sales_result(
                    lookup,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source=(
                        "technical_fallback"
                        if lookup.safety_reason in {
                            "tray_adapter_unavailable",
                            "product_match_failed",
                        }
                        else "deterministic_fallback"
                    ),
                    used_openai_responder=False,
                    used_tray=bool(lookup.response_metadata.get("used_tray", True)),
                    fallback_reason=lookup.safety_reason,
                ),
            )
    if (
        interpretation is not None
        and interpretation.checkout_channel_preference is not None
        and interpretation.purchase_action != "create_cart"
        and interpretation.payment_action is None
    ):
        channel_result = sales.select_checkout_channel(
            state,
            interpretation.checkout_channel_preference,
        )
        if (
            interpretation.checkout_channel_preference == "whatsapp"
            and state.checkout_channel_preference == "whatsapp"
        ):
            channel_result = await sales._advance_whatsapp_checkout(
                state,
                channel_result,
                state.payment_method_preference,
                interpretation.installment_count,
            )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            channel_result,
            interpretation,
        )
        if final:
            return CatalogPendingResolution(
                interpretation=interpretation,
                plan=plan,
                purchase_action=purchase_action,
                resolved_product=resolved_product,
                resolved_by=resolved_by,
                purchase_requests=purchase_requests,
                unresolved_purchase_items=unresolved_purchase_items,
                unresolved_candidates=unresolved_candidates,
                pending_link_requested=pending_link_requested,
                pending_action_used=pending_action_used,
                early_result=final,
            )
        return CatalogPendingResolution(
            interpretation=interpretation,
            plan=plan,
            purchase_action=purchase_action,
            resolved_product=resolved_product,
            resolved_by=resolved_by,
            purchase_requests=purchase_requests,
            unresolved_purchase_items=unresolved_purchase_items,
            unresolved_candidates=unresolved_candidates,
            pending_link_requested=pending_link_requested,
            pending_action_used=pending_action_used,
            early_result=sales._mark_sales_result(
                channel_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=channel_result.safety_reason,
            ),
        )
    return CatalogPendingResolution(
        interpretation=interpretation,
        plan=plan,
        purchase_action=purchase_action,
        resolved_product=resolved_product,
        resolved_by=resolved_by,
        purchase_requests=purchase_requests,
        unresolved_purchase_items=unresolved_purchase_items,
        unresolved_candidates=unresolved_candidates,
        pending_link_requested=pending_link_requested,
        pending_action_used=pending_action_used,
    )
