"""Create-cart, payment, inspect-cart, and cart-link replies.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


async def try_catalog_purchase(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    purchase_action: str | None,
    resolved_product: Any,
    purchase_requests: list[Any],
    unresolved_purchase_items: int,
    unresolved_candidates: list[dict[str, Any]],
) -> AgentResult | None:
    sales = _sales()
    payment_preference = (
        interpretation.payment_method_preference
        if interpretation is not None
        else None
    )
    payment_requested = bool(
        interpretation
        and (interpretation.payment_action or payment_preference)
    )
    if purchase_action == "create_cart":
        target_count = len(purchase_requests) + (
            1 if resolved_product is not None else 0
        )
        print("[sales.action.guard]", {
            "action": "create_cart",
            "target_count": target_count,
            "allowed": target_count > 0,
            "blocking_reason": (
                None if target_count > 0 else "product_target_missing"
            ),
        })
    needs_cart = bool(
        payment_requested
        and (
            purchase_action in {"create_cart", "checkout_question", "show_cart_link"}
            or purchase_requests
        )
        and (
            not (state.cart_session_id and state.cart_url)
            or purchase_action == "create_cart"
            or bool(purchase_requests)
        )
    )
    print("[sales.purchase.orchestrator.decision]", {
        "intent": plan.get("intent"),
        "purchase_action": purchase_action,
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(purchase_requests),
        "has_cart_session": bool(state.cart_session_id),
        "needs_cart": needs_cart,
        "payment_requested": payment_requested,
    })
    if (
        not payment_requested
        and purchase_action in {"checkout_question", "show_cart_link"}
        and not state.cart_session_id
    ):
        if unresolved_purchase_items or (
            not purchase_requests and resolved_product is None
        ):
            missing = sales._purchase_product_required_result(state)
            return sales._mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        _ensured_state, ensured_result = await sales._ensure_cart_for_purchase(
            interpretation=interpretation,
            state=state,
            purchase_requests=purchase_requests,
            resolved_product=resolved_product,
        )
        if ensured_result is not None:
            final = await sales._sales_response_with_openai(
                message,
                plan,
                ensured_result,
                interpretation,
            )
            if final:
                return final
            return sales._mark_sales_result(
                ensured_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if ensured_result.safety_reason == "cart_technical_failure"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=bool(ensured_result.response_metadata.get("used_tray", True)),
                fallback_reason=ensured_result.safety_reason,
            )
    if payment_requested:
        informational_payment = sales._is_informational_payment_query(
            interpretation,
            purchase_action=purchase_action,
            has_purchase_requests=bool(purchase_requests),
        )
        if informational_payment and not (
            state.cart_session_id and state.cart_url
        ):
            from app.persona.persona_runtime import get_persona_runtime

            runtime = get_persona_runtime()
            force_cart = bool(
                runtime and runtime.require_cart_for_informational_payment
            )
            if force_cart:
                missing = sales._purchase_product_required_result(state)
                return sales._mark_sales_result(
                    missing,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason=missing.safety_reason,
                )
            print("[sales.purchase.orchestrator.decision]", {
                "branch": "informational_payment_no_cart",
                "payment_request_kind": (
                    interpretation.payment_request_kind
                    if interpretation is not None
                    else None
                ),
                "persona_policy_source": (
                    runtime.policy_source if runtime is not None else None
                ),
            })
            info_result = sales._informational_payment_policy_result(
                state,
                payment_method_preference=payment_preference,
            )
            final = await sales._sales_response_with_openai(
                message,
                plan,
                info_result,
                interpretation,
            )
            if final:
                return final
            return sales._mark_sales_result(
                info_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=info_result.safety_reason,
            )

        if unresolved_purchase_items:
            missing = sales._purchase_product_required_result(state)
            return sales._mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_state = state
        cart_result: AgentResult | None = None
        if needs_cart:
            payment_state, cart_result = await sales._ensure_cart_for_purchase(
                interpretation=interpretation,
                state=state,
                purchase_requests=purchase_requests,
                resolved_product=resolved_product,
            )
            if cart_result is not None and not (
                payment_state.cart_session_id
                and payment_state.cart_url
            ):
                return sales._mark_sales_result(
                    cart_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source=(
                        "technical_fallback"
                        if cart_result.safety_reason == "cart_technical_failure"
                        else "deterministic_fallback"
                    ),
                    used_openai_responder=False,
                    used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
                    fallback_reason=cart_result.safety_reason,
                )
        if not (
            payment_state.cart_session_id
            and payment_state.cart_url
        ):
            from app.persona.persona_runtime import get_persona_runtime

            runtime = get_persona_runtime()
            if runtime is not None and not runtime.require_product_before_checkout:
                info_result = sales._informational_payment_policy_result(
                    state,
                    payment_method_preference=payment_preference,
                )
                final = await sales._sales_response_with_openai(
                    message,
                    plan,
                    info_result,
                    interpretation,
                )
                if final:
                    return final
                return sales._mark_sales_result(
                    info_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=False,
                    fallback_reason=info_result.safety_reason,
                )
            missing = sales._purchase_product_required_result(state)
            return sales._mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_result = await sales.inspect_payment_options(
            state=payment_state,
            installment_count=interpretation.installment_count,
            payment_method_preference=payment_preference,
            execute=sales.execute_tool,
            payment_option_id=interpretation.payment_option_id,
            advance_checkout=bool(
                payment_preference is not None
                or interpretation.payment_request_kind == "checkout"
                or purchase_action == "checkout_question"
            ),
            reconciled_cart=(
                {
                    **((cart_result.commercial_data or {}).get("cart") or {}),
                    "items": (
                        (cart_result.response_metadata.get("cart_state") or {}).get(
                            "cart_items", []
                        )
                    ),
                }
                if cart_result is not None
                else None
            ),
        )
        if payment_preference is not None:
            payment_result.response_metadata["payment_method_preference"] = (
                payment_preference
            )
        combined_result = (
            sales._combine_cart_and_payment_results(cart_result, payment_result)
            if cart_result is not None
            else payment_result
        )
        payment_changed_during_review = bool(
            state.order_confirmation_pending
            and payment_preference is not None
            and payment_preference != state.payment_method_preference
        )
        refreshed_payment_state = sales.evolve_commerce_state(payment_state, combined_result)
        if (
            payment_changed_during_review
            and refreshed_payment_state.selected_payment_option is not None
        ):
            review_result = await sales.prepare_order(
                state=refreshed_payment_state,
                execute=sales.execute_tool,
            )
            combined_result = sales._combine_checkout_and_followup_results(
                combined_result, review_result,
            )
        if interpretation.checkout_channel_preference is not None:
            channel_result = sales.select_checkout_channel(
                payment_state,
                interpretation.checkout_channel_preference,
            )
            combined_result = sales._combine_checkout_channel_result(
                combined_result,
                channel_result,
            )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            combined_result,
            interpretation,
            sales.evolve_commerce_state(payment_state, combined_result),
        )
        if final:
            return final
        return sales._mark_sales_result(
            combined_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if payment_result.safety_reason == "payment_options_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(combined_result.response_metadata.get("used_tray")),
            fallback_reason=payment_result.safety_reason,
        )
    if purchase_action == "inspect_cart":
        cart_result = await sales.inspect_current_cart(state=state, execute=sales.execute_tool)
        final = await sales._sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        if final:
            return final
        return sales._mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray")),
            fallback_reason=cart_result.safety_reason,
        )
    if purchase_action in {"show_cart_link", "checkout_question"}:
        cart_result = sales.current_cart_reply(
            state,
            checkout_question=purchase_action == "checkout_question",
        )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return sales._mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="sales_responder_unavailable",
        )
    if purchase_action == "create_cart" and unresolved_purchase_items:
        sales.log_purchase_progress(
            "product_resolution",
            "blocked",
            "purchase_item_unresolved",
        )
        return sales._mark_sales_result(
            AgentResult(
                reply_text="Encontrei mais de uma possibilidade. Confirme quais itens da lista devem entrar no carrinho.",
                intent="commerce",
                handoff_required=False,
                safety_reason="ambiguous_purchase_item",
                commercial_data={
                    "products": unresolved_candidates or [
                        item.model_dump(mode="json")
                        for item in state.last_presented_products
                    ],
                    "cart": {"status": "item_clarification_required"},
                },
                response_metadata={"presented_products": bool(unresolved_candidates)},
            ),
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_item_unresolved",
        )
    if purchase_action == "create_cart" and (purchase_requests or resolved_product is not None):
        if purchase_requests:
            cart_result = await sales.create_cart_items_checkout(
                item_requests=purchase_requests,
                state=state,
                execute=sales.execute_tool,
            )
        else:
            cart_result = await sales.create_cart_checkout(
                interpretation=interpretation,
                product_reference=resolved_product,
                state=state,
                execute=sales.execute_tool,
            )
        if (
            cart_result.safety_reason is None
            and interpretation.checkout_channel_preference is not None
        ):
            checkout_state = sales.evolve_commerce_state(state, cart_result)
            channel_result = sales.select_checkout_channel(
                checkout_state,
                interpretation.checkout_channel_preference,
            )
            cart_result = sales._combine_checkout_channel_result(
                cart_result,
                channel_result,
            )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return sales._mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
            fallback_reason=cart_result.safety_reason or "sales_responder_unavailable",
        )
    return None
