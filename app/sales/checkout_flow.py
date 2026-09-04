"""Cart / checkout / order orchestration helpers (IQ-08)."""

from __future__ import annotations

from typing import Any

from app.commerce.cart_service import (
    CartItemRequest,
    create_cart_checkout,
    create_cart_items_checkout,
)
from app.commerce.commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    checkout_missing_fields,
    evolve_commerce_state,
)
from ..models import AgentResult, IncomingMessage, SalesInterpretation
from app.commerce.order_service import confirm_prepared_order, create_order, prepare_order
from app.commerce.payment_service import inspect_order_payment, inspect_payment_options
from app.commerce.pix_checkout_service import (
    generate_direct_pix_checkout,
    should_use_direct_pix,
)
from app.catalog.product_retrieval import commercial_availability_facts
from app.commerce.shipping_service import quote_shipping, select_shipping
from .result_utils import mark_sales_result


async def _call_execute_tool(name: str, arguments: dict[str, Any]):
    """Prefer sales_agent.execute_tool so tests can monkeypatch it."""
    from app import sales_agent

    return await sales_agent.execute_tool(name, arguments)


async def _confirm_current_order_review(
    *,
    message: IncomingMessage,
    plan: dict[str, Any],
    state: CommerceConversationState,
    source: str,
) -> AgentResult:
    print("[sales.order.confirmation.turn]", {
        "pending_action_before": state.pending_action,
        "confirmation_source": source,
        "explicit_change_detected": False,
        "review_version_present": bool(state.order_review_version),
        "confirmed_review_version_present": bool(state.confirmed_order_review_version),
        "branch_taken": "confirm_order_review",
        "prepare_order_called": False,
        "confirm_prepared_order_called": True,
        "create_order_called": True,
    })
    confirmed = confirm_prepared_order(state)
    confirmed_state = evolve_commerce_state(state, confirmed)
    order_result = await _fulfill_confirmed_order(confirmed_state, message=message)
    final_state = evolve_commerce_state(confirmed_state, order_result)
    print("[sales.order.confirmation.turn]", {
        "pending_action_after": final_state.pending_action,
        "branch_taken": (
            "order_created"
            if final_state.order_id
            else "pix_pending"
            if final_state.pix_payment_id
            else "order_not_created"
        ),
    })
    return await respond_to_commerce_service(
        message=message,
        plan=plan,
        result=order_result,
        interpretation=None,
        state=final_state,
    )


async def _ensure_cart_for_purchase(
    *,
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
    purchase_requests: list[CartItemRequest],
    resolved_product: CommerceProductReference | None,
) -> tuple[CommerceConversationState, AgentResult | None]:
    if (
        state.cart_session_id
        and state.cart_url
        and not purchase_requests
        and resolved_product is None
    ):
        print("[sales.purchase.ensure_cart]", {
            "cart_existed": True,
            "cart_created": False,
            "item_count": len(state.cart_items),
        })
        return state, None

    if purchase_requests:
        cart_result = await create_cart_items_checkout(
            item_requests=purchase_requests,
            state=state,
            execute=_call_execute_tool,
        )
    elif resolved_product is not None:
        cart_result = await create_cart_checkout(
            interpretation=interpretation,
            product_reference=resolved_product,
            state=state,
            execute=_call_execute_tool,
        )
    else:
        print("[sales.purchase.ensure_cart]", {
            "cart_existed": False,
            "cart_created": False,
            "item_count": 0,
        })
        return state, None

    updated_state = evolve_commerce_state(state, cart_result)
    if (
        cart_result.response_metadata.get("cart_materially_changed") is True
        and updated_state.checkout_channel_preference == "whatsapp"
        and updated_state.checkout_draft.address.zip_code
    ):
        quote_result = await quote_shipping(
            state=updated_state,
            zipcode=updated_state.checkout_draft.address.zip_code,
            execute=_call_execute_tool,
        )
        cart_result = _combine_checkout_and_followup_results(
            cart_result,
            quote_result,
        )
        updated_state = evolve_commerce_state(state, cart_result)
    print("[sales.purchase.ensure_cart]", {
        "cart_existed": False,
        "cart_created": bool(
            updated_state.cart_session_id
            and updated_state.cart_url
        ),
        "item_count": len(updated_state.cart_items),
    })
    return updated_state, cart_result


def _combine_cart_and_payment_results(
    cart_result: AgentResult,
    payment_result: AgentResult,
) -> AgentResult:
    commercial_data = dict(cart_result.commercial_data or {})
    for key, value in (payment_result.commercial_data or {}).items():
        if key == "cart" and key in commercial_data:
            continue
        commercial_data[key] = value
    metadata = dict(cart_result.response_metadata or {})
    metadata.update(payment_result.response_metadata or {})
    if "cart_state" in cart_result.response_metadata:
        metadata["cart_state"] = cart_result.response_metadata["cart_state"]
    metadata["purchase_stage"] = "payment_discussion"

    return AgentResult(
        reply_text=payment_result.reply_text,
        intent="commerce",
        handoff_required=False,
        safety_reason=payment_result.safety_reason or cart_result.safety_reason,
        commercial_data=commercial_data,
        response_metadata=metadata,
    )


def _combine_checkout_channel_result(
    base_result: AgentResult,
    channel_result: AgentResult,
) -> AgentResult:
    commercial_data = dict(base_result.commercial_data or {})
    commercial_data.update(channel_result.commercial_data or {})
    metadata = dict(base_result.response_metadata or {})
    metadata.update(channel_result.response_metadata or {})
    if "cart_state" in base_result.response_metadata:
        metadata["cart_state"] = base_result.response_metadata["cart_state"]
    return AgentResult(
        reply_text=channel_result.reply_text,
        intent="commerce",
        handoff_required=False,
        safety_reason=channel_result.safety_reason or base_result.safety_reason,
        commercial_data=commercial_data,
        response_metadata=metadata,
    )


async def respond_to_commerce_service(
    *,
    message: IncomingMessage,
    plan: dict[str, Any],
    result: AgentResult,
    interpretation: SalesInterpretation,
    state: CommerceConversationState | None = None,
) -> AgentResult:
    from app.sales_agent import _sales_response_with_openai

    final = await _sales_response_with_openai(
        message, plan, result, interpretation, state,
    )
    if final:
        return final
    return mark_sales_result(
        result,
        interpretation=interpretation,
        goal=plan.get("goal"),
        response_source=(
            "technical_fallback"
            if result.safety_reason and "technical_failure" in result.safety_reason
            else "deterministic_fallback"
        ),
        used_openai_responder=False,
        used_tray=bool(result.response_metadata.get("used_tray")),
        fallback_reason=result.safety_reason,
    )


def _combine_checkout_and_followup_results(
    checkout_result: AgentResult,
    followup_result: AgentResult,
) -> AgentResult:
    """Keep persisted checkout updates while continuing the same customer turn."""
    commercial_data = dict(checkout_result.commercial_data or {})
    commercial_data.update(followup_result.commercial_data or {})
    metadata = dict(checkout_result.response_metadata or {})
    followup_metadata = followup_result.response_metadata or {}
    nested_metadata: dict[str, dict[str, Any]] = {}
    for key in ("cart_state", "shipping_state", "checkout_state", "order_state", "payment_state"):
        if isinstance(metadata.get(key), dict) and isinstance(followup_metadata.get(key), dict):
            nested_metadata[key] = {**metadata[key], **followup_metadata[key]}
    metadata.update(followup_metadata)
    metadata.update(nested_metadata)
    return AgentResult(
        reply_text=followup_result.reply_text,
        intent="commerce",
        handoff_required=False,
        safety_reason=followup_result.safety_reason or checkout_result.safety_reason,
        commercial_data=commercial_data,
        response_metadata=metadata,
    )


async def _advance_whatsapp_checkout(
    state: CommerceConversationState,
    result: AgentResult,
    payment_preference: str | None,
    installment_count: int | None,
) -> AgentResult:
    """Advance factual WhatsApp steps without asking the customer to confirm each one."""
    current = evolve_commerce_state(state, result)
    cart_snapshot: dict[str, Any] | None = None
    if current.checkout_channel_preference != "whatsapp":
        return result
    if checkout_missing_fields(current.checkout_draft):
        return result
    zipcode = current.checkout_draft.address.zip_code
    if zipcode and not current.shipping_quotes:
        shipping_result = await quote_shipping(
            state=current, zipcode=zipcode, execute=_call_execute_tool,
        )
        snapshot = shipping_result.response_metadata.get("cart_snapshot")
        cart_snapshot = snapshot if isinstance(snapshot, dict) else None
        result = _combine_checkout_and_followup_results(result, shipping_result)
        current = evolve_commerce_state(state, result)
    if len(current.shipping_quotes) == 1 and current.selected_shipping is None:
        shipping_result = select_shipping(current, selection_position=1)
        result = _combine_checkout_and_followup_results(result, shipping_result)
        current = evolve_commerce_state(state, result)
    if (
        payment_preference
        and not checkout_missing_fields(current.checkout_draft)
        and current.selected_shipping is not None
    ):
        payment_result = await inspect_payment_options(
            state=current,
            installment_count=installment_count,
            payment_method_preference=payment_preference,
            execute=_call_execute_tool,
            advance_checkout=True,
            reconciled_cart=cart_snapshot,
        )
        result = _combine_checkout_and_followup_results(result, payment_result)
        current = evolve_commerce_state(state, result)
    if (
        current.selected_shipping is not None
        and current.selected_payment_option is not None
        and not checkout_missing_fields(current.checkout_draft)
        and not current.order_confirmation_pending
    ):
        order_result = await prepare_order(
            state=current, execute=_call_execute_tool, cart_snapshot=cart_snapshot,
        )
        result = _combine_checkout_and_followup_results(result, order_result)
    return result


def _combine_order_and_payment_results(
    order_result: AgentResult,
    payment_result: AgentResult,
) -> AgentResult:
    order_facts = dict(order_result.commercial_data or {})
    payment_facts = payment_result.commercial_data or {}
    order_facts["payment"] = payment_facts.get("payment", {
        "status": "unknown",
        "has_payment": None,
        "payment_url_available": False,
    })
    metadata = dict(order_result.response_metadata or {})
    metadata.update(payment_result.response_metadata or {})
    if "order_state" in order_result.response_metadata:
        metadata["order_state"] = order_result.response_metadata["order_state"]
    payment = order_facts.get("payment") if isinstance(order_facts.get("payment"), dict) else {}
    payment_url = payment.get("payment_url")
    status = str(
        order_facts.get("status")
        or payment.get("status")
        or "em processamento"
    ).strip()
    order_id = order_facts.get("order_id")
    if payment_url:
        reply_text = (
            f'Seu pedido {order_id} está com status "{status}". '
            f"Segue o link para pagamento: {payment_url}"
        )
    else:
        reply_text = (
            payment_result.reply_text
            if payment_result.reply_text
            and "factual" not in payment_result.reply_text.casefold()
            else f'Seu pedido {order_id} está com status "{status}".'
        )
    metadata["factual_fallback_text"] = reply_text
    return AgentResult(
        reply_text=reply_text,
        intent="commerce",
        safety_reason=payment_result.safety_reason,
        commercial_data=order_facts,
        response_metadata=metadata,
    )


def _order_payment_revalidation(
    state: CommerceConversationState,
    payment_options_result: dict[str, Any],
) -> str:
    """Confirm the order's payment choice without selecting a different gateway."""
    options = payment_options_result.get("payment_options")
    if not isinstance(options, dict) or payment_options_result.get("error"):
        return "not_checked"
    selected = state.selected_payment_option
    preference = state.selected_payment_method or state.payment_method_preference
    values = options.get("options")
    values = [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    selected_id = state.selected_payment_option_id or (selected.id if selected else None)
    if selected_id is not None:
        id_matches = [item for item in values if str(item.get("id")) == str(selected_id)]
        if len(id_matches) == 1:
            return "confirmed"
        if len(id_matches) > 1:
            return "ambiguous"
    selected_integration = selected.integration_code if selected else None
    if selected_integration:
        integration_matches = [
            item for item in values
            if item.get("integration_code") is not None
            and str(item["integration_code"]) == str(selected_integration)
        ]
        if len(integration_matches) == 1:
            return "confirmed"
        if len(integration_matches) > 1:
            return "ambiguous"
    if preference not in {"pix", "card", "boleto", "other"}:
        return "not_checked"
    semantic_candidates: list[dict[str, Any]] = []
    for item in values:
        label = " ".join(str(item.get(key) or "") for key in ("name", "text", "method")).casefold()
        if preference == "card" and str(item.get("card")) == "1":
            semantic_candidates.append(item)
        elif preference in {"pix", "boleto"} and preference in label:
            semantic_candidates.append(item)
    if len(semantic_candidates) == 1:
        return "confirmed"
    return "ambiguous" if len(semantic_candidates) > 1 else "unavailable"


async def _fulfill_confirmed_order(
    state: CommerceConversationState,
    *,
    message: IncomingMessage | None = None,
) -> AgentResult:
    """After explicit order confirmation: direct PIX (if enabled) or Tray order+link."""
    if should_use_direct_pix(state):
        return await generate_direct_pix_checkout(
            state=state,
            execute=_call_execute_tool,
            conversation_id=message.conversation_id if message else None,
            sender_key=message.sender_key if message else None,
            sender_phone=message.sender_phone if message else None,
            channel=message.channel if message else None,
        )
    return await _create_order_with_payment_lookup(state)


async def _create_order_with_payment_lookup(
    state: CommerceConversationState,
) -> AgentResult:
    order_result = await create_order(state=state, execute=_call_execute_tool)
    order_id = (order_result.commercial_data or {}).get("order_id")
    if not order_id or not (order_result.commercial_data or {}).get("success"):
        return order_result
    created_state = evolve_commerce_state(state, order_result)
    try:
        order_payment_options = await _call_execute_tool(
            "get_payment_options", {"order_id": str(order_id)},
        )
    except Exception as exc:
        order_payment_options = {
            "error": "commerce_upstream_error", "error_type": type(exc).__name__,
        }
    revalidation_status = _order_payment_revalidation(
        created_state, order_payment_options,
    )
    payment_result = await inspect_order_payment(
        state=created_state,
        execute=_call_execute_tool,
        order_id=str(order_id),
    )
    if not (payment_result.commercial_data or {}).get("payment", {}).get("payment_url"):
        refreshed_state = evolve_commerce_state(created_state, payment_result)
        payment_result = await inspect_order_payment(
            state=refreshed_state,
            execute=_call_execute_tool,
            order_id=str(order_id),
        )
    combined = _combine_order_and_payment_results(order_result, payment_result)
    combined.commercial_data = {
        **(combined.commercial_data or {}),
        "order_payment_options": order_payment_options.get("payment_options"),
        "order_payment_options_checked": "error" not in order_payment_options,
        "order_payment_revalidation_status": revalidation_status,
    }
    payment_state = dict(combined.response_metadata.get("payment_state") or {})
    payment_state["order_payment_revalidation_status"] = revalidation_status
    combined.response_metadata["payment_state"] = payment_state
    return combined


def _pending_product_references(
    state: CommerceConversationState,
) -> list[CommerceProductReference]:
    by_id: dict[str, CommerceProductReference] = {}
    if state.active_product is not None:
        by_id[state.active_product.product_id] = state.active_product
    for product in state.last_presented_products:
        by_id[product.product_id] = CommerceProductReference.model_validate(
            product.model_dump(exclude={"position"})
        )
    return [
        by_id[product_id]
        for product_id in state.pending_action_product_ids
        if product_id in by_id
    ]


async def _inspect_listed_products(
    state: CommerceConversationState,
) -> AgentResult:
    products: list[dict[str, Any]] = []
    for item in state.last_presented_products[:3]:
        raw = await _call_execute_tool("get_product", {"product_id": item.product_id})
        if "error" in raw:
            continue
        product = dict(raw)
        product["id"] = product.get("id") or item.product_id
        product["name"] = product.get("name") or item.name
        product["brand"] = product.get("brand") or item.brand
        product["commercial_availability"] = commercial_availability_facts(product)
        products.append(product)
    if not products:
        return AgentResult(
            reply_text="Não consegui consultar agora os modelos que acabei de listar.",
            intent="commerce",
            handoff_required=False,
            safety_reason="tray_adapter_unavailable",
            response_metadata={"domain": "commerce"},
        )
    lines = []
    for position, product in enumerate(products, start=1):
        facts = product.get("commercial_availability") or {}
        if facts.get("in_ready_to_ship_category") or facts.get(
            "immediate_delivery_supported"
        ):
            status = "pronta entrega"
        elif facts.get("lead_time_days"):
            status = f"sob encomenda, cerca de {facts['lead_time_days']} dias úteis"
        else:
            status = str(product.get("availability") or "prazo sob consulta")
        name = product.get("name") or f"opção {position}"
        lines.append(f"{position}. {name} — {status}")
    return AgentResult(
        reply_text=(
            "Sobre os modelos que acabei de listar:\n"
            + "\n".join(lines)
        ),
        intent="commerce",
        handoff_required=False,
        commercial_data={"products": products},
        response_metadata={
            "domain": "commerce",
            "presented_products": True,
            "product_resolution_state": "options_presented",
            "used_tray": True,
        },
    )


def _pending_action_rejected_result(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> AgentResult:
    print("[sales.pending_action]", {
        "action": state.pending_action,
        "has_product": bool(_pending_product_references(state)),
        "confirmation": interpretation.confirmation,
        "executed": False,
    })
    print("[sales.state.application]", {
        "had_pending_action": True,
        "pending_action_used": False,
        "pending_action_cleared": True,
        "had_active_product": state.active_product is not None,
        "active_product_referenced": False,
    })
    interpretation._clear_pending_action = True
    return mark_sales_result(
        AgentResult(
            reply_text="Tudo bem. Não vou executar essa ação.",
            intent="commerce",
            handoff_required=False,
            response_metadata={
                "clear_pending_action": True,
                **(
                    {
                        "order_state": {
                            "order_confirmation_status": "not_ready",
                            "order_review_version": None,
                            "confirmed_order_review_version": None,
                        }
                    }
                    if state.pending_action == "awaiting_order_confirmation"
                    else {}
                ),
            },
        ),
        interpretation=interpretation,
        goal=interpretation.goal,
        response_source="deterministic_fallback",
        used_openai_responder=False,
        used_tray=False,
        fallback_reason="pending_action_rejected",
    )




_respond_to_commerce_service = respond_to_commerce_service
_confirm_current_order_review = _confirm_current_order_review
_ensure_cart_for_purchase = _ensure_cart_for_purchase
