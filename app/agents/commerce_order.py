"""Order review, payment, checkout and shipping routes for the sales handler.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from typing import Any

from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


async def try_commerce_confirmation(
    message: IncomingMessage,
    state: Any,
) -> Any | None:
    sales = _sales()
    deterministic_confirmation = sales._confirmation_text_kind(state, message.text)
    if deterministic_confirmation == "confirm":
        return await sales._confirm_current_order_review(
            message=message,
            plan={"intent": "commerce", "goal": "buy"},
            state=state,
            source="contextual_text",
        )
    if deterministic_confirmation != "reject":
        return None
    rejected = AgentResult(
        reply_text="A confirmação do pedido foi cancelada.",
        intent="commerce",
        response_metadata={
            "domain": "commerce",
            "clear_pending_action": True,
            "order_state": {
                "order_confirmation_status": "not_ready",
                "order_review_version": None,
                "confirmed_order_review_version": None,
            },
            "used_tray": False,
        },
    )
    print("[sales.order.confirmation.turn]", {
        "pending_action_before": state.pending_action,
        "confirmation_source": "contextual_text",
        "explicit_change_detected": False,
        "review_version_present": bool(state.order_review_version),
        "confirmed_review_version_present": bool(state.confirmed_order_review_version),
        "branch_taken": "reject_order_review",
        "prepare_order_called": False,
        "confirm_prepared_order_called": False,
        "create_order_called": False,
        "pending_action_after": None,
    })
    return await sales._respond_to_commerce_service(
        message=message,
        plan={"intent": "commerce", "goal": "buy"},
        result=rejected,
        interpretation=None,
        state=sales.evolve_commerce_state(state, rejected),
    )


def try_commerce_objection(
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    state: Any,
) -> Any | None:
    sales = _sales()
    from app.sales.policies.objection_authority import try_objection_authority_result

    objection_result = try_objection_authority_result(message, interpretation, state)
    if objection_result is None:
        return None
    print("[sales.objection.authority]", {
        "kind": (objection_result.response_metadata or {}).get("objection_kind"),
        "handoff": objection_result.handoff_required,
    })
    return sales._mark_sales_result(
        objection_result,
        interpretation=interpretation,
        goal=(interpretation.goal if interpretation is not None else "discover"),
        response_source="deterministic_objection",
        used_openai_responder=False,
        used_tray=False,
        fallback_reason=objection_result.safety_reason,
    )


async def try_commerce_checkout_routes(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: Any,
) -> Any | None:
    sales = _sales()
    if interpretation is not None and interpretation.payment_action == "order_payment":
        if state.pix_payment_id and not state.order_id:
            payment_result = await sales.refresh_direct_pix_checkout(state=state)
        else:
            payment_result = await sales.inspect_order_payment(
                state=state,
                execute=sales.execute_tool,
                order_id=interpretation.order_id,
            )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=payment_result,
            interpretation=interpretation,
            state=sales.evolve_commerce_state(state, payment_result),
        )
    if interpretation is not None and interpretation.order_action is not None:
        order_result = await sales.get_order_facts(
            state=state,
            execute=sales.execute_tool,
            order_id=interpretation.order_id,
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=sales.evolve_commerce_state(state, order_result),
        )
    if (
        interpretation is not None
        and state.pending_action == "awaiting_order_confirmation"
        and interpretation.confirmation == "confirm"
        and interpretation.purchase_action not in {
            "set_cart_item_quantity", "remove_cart_item",
        }
        and interpretation.checkout_data is None
        and interpretation.shipping_action is None
        and interpretation.payment_action is None
        and interpretation.checkout_channel_preference is None
        and not interpretation.domain_change_explicit
    ):
        confirmed = sales.confirm_prepared_order(state)
        confirmed_state = sales.evolve_commerce_state(state, confirmed)
        order_result = await sales._fulfill_confirmed_order(
            confirmed_state, message=message,
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=sales.evolve_commerce_state(confirmed_state, order_result),
        )
    if interpretation is not None and interpretation.checkout_data is not None:
        checkout_updates = interpretation.checkout_data.model_dump(
            mode="json",
            exclude_none=True,
        )
        checkout_result = sales.update_checkout_data(
            state,
            checkout_updates,
        )
        field_errors = checkout_result.commercial_data.get("field_errors")
        field_errors = field_errors if isinstance(field_errors, dict) else {}
        missing_fields = checkout_result.commercial_data.get("missing_fields")
        missing_fields = missing_fields if isinstance(missing_fields, list) else []
        enriched_updates = await sales.enrich_checkout_data_from_cep(
            checkout_updates,
            known_zipcode=state.checkout_draft.address.zip_code,
            missing_fields=missing_fields,
            field_errors=field_errors,
        )
        cep_resolution_applied = enriched_updates != checkout_updates
        if cep_resolution_applied:
            checkout_updates = enriched_updates
            checkout_result = sales.update_checkout_data(state, checkout_updates)
            field_errors = checkout_result.commercial_data.get("field_errors")
            field_errors = field_errors if isinstance(field_errors, dict) else {}
            missing_fields = checkout_result.commercial_data.get("missing_fields")
            missing_fields = missing_fields if isinstance(missing_fields, list) else []
        repair_attempted = sales.should_repair_checkout_data(
            message.text,
            checkout_updates,
            missing_fields,
            field_errors,
        )
        if repair_attempted:
            repaired_updates = await sales.repair_checkout_data_with_openai(
                message_text=message.text,
                updates=checkout_updates,
                missing_fields=missing_fields,
                field_errors=field_errors,
            )
            if repaired_updates != checkout_updates:
                checkout_result = sales.update_checkout_data(state, repaired_updates)
            checkout_result.response_metadata["checkout_data_repair_attempted"] = True
            checkout_result.response_metadata["checkout_data_repair_applied"] = (
                repaired_updates != checkout_updates
            )
        checkout_result.response_metadata["checkout_cep_resolution_applied"] = (
            cep_resolution_applied
        )
        payment_preference = (
            interpretation.payment_method_preference
            or state.payment_method_preference
        )
        if payment_preference is not None:
            checkout_result.response_metadata["payment_method_preference"] = (
                payment_preference
            )
        checkout_result = await sales._advance_whatsapp_checkout(
            state,
            checkout_result,
            payment_preference,
            interpretation.installment_count,
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=checkout_result,
            interpretation=interpretation,
            state=sales.evolve_commerce_state(state, checkout_result),
        )
    if interpretation is not None and interpretation.shipping_action == "quote":
        shipping_result = await sales.quote_shipping(
            state=state,
            zipcode=interpretation.shipping_zipcode or "",
            execute=sales.execute_tool,
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.shipping_action == "list_methods":
        shipping_result = await sales.list_shipping_methods(execute=sales.execute_tool)
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.shipping_action == "select":
        shipping_result = sales.select_shipping(
            state,
            selection_id=interpretation.shipping_selection_id,
            selection_position=interpretation.shipping_selection_position,
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if (
        interpretation is not None
        and interpretation.confirmation == "confirm"
        and state.pending_action == "awaiting_shipping_selection"
        and len(state.shipping_quotes) == 1
    ):
        shipping_result = sales.select_shipping(state, selection_position=1)
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=shipping_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.checkout_action == "prepare_order":
        order_result = await sales.prepare_order(state=state, execute=sales.execute_tool)
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
        )
    if interpretation is not None and interpretation.checkout_action == "create_order":
        order_result = await sales._fulfill_confirmed_order(state, message=message)
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=order_result,
            interpretation=interpretation,
            state=sales.evolve_commerce_state(state, order_result),
        )
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "reject"
    ):
        return sales._pending_action_rejected_result(interpretation, state)
    return None
