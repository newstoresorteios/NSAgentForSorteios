"""Cart item remove/quantity, outbound image, and product-link replies.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

from typing import Any

from app.commerce.commerce_context import CommerceConversationState, CommerceProductReference
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


def _official_catalog_image_url(result: AgentResult) -> str | None:
    meta = result.response_metadata or {}
    candidates = [meta.get("outbound_image_url")]
    image = (result.commercial_data or {}).get("image")
    if isinstance(image, dict):
        candidates.append(image.get("url"))
    images = (result.commercial_data or {}).get("images")
    if isinstance(images, list):
        candidates.extend(
            item.get("url")
            for item in images
            if isinstance(item, dict)
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _dead_product_link_reply(
    *,
    resolved_product: Any,
    link_facts: dict[str, Any],
) -> str:
    parts = ["Não consegui o link agora."]
    reference = link_facts.get("reference") or getattr(resolved_product, "reference", None)
    price = (
        link_facts.get("current_price")
        or link_facts.get("price")
        or getattr(resolved_product, "current_price", None)
    )
    facts: list[str] = []
    if isinstance(reference, str) and reference.strip():
        facts.append(f"referência {reference.strip()}")
    if price is not None and str(price).strip():
        facts.append(f"valor {str(price).strip()}")
    if facts:
        parts.append("Tenho " + " e ".join(facts) + ".")
    return " ".join(parts)


async def try_remove_cart_item(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    purchase_action: str | None,
    resolved_product: Any,
) -> AgentResult | None:
    if purchase_action != "remove_cart_item":
        return None
    sales = _sales()
    if not state.cart_session_id:
        removal_result = AgentResult(
            reply_text="",
            intent="commerce",
            safety_reason="cart_item_removal_cart_invalid",
            commercial_data={
                "removal_requested": True,
                "removal_supported": False,
            },
            response_metadata={
                "domain": "commerce",
                "clear_pending_action": True,
                "used_tray": False,
            },
        )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=removal_result,
            interpretation=interpretation,
        )
    targets, resolution_reason = sales.resolve_cart_item_reference(
        interpretation, state, resolved_product
    )
    if not targets:
        if resolution_reason == "ambiguous":
            removal_result = AgentResult(
                reply_text="",
                intent="commerce",
                safety_reason="cart_item_removal_ambiguous",
                commercial_data={
                    "removal_requested": True,
                    "removal_supported": True,
                    "mutation_success": False,
                    "reason": "ambiguous",
                    "cart_items": [
                        {
                            "position": i + 1,
                            "product_id": item.product_id,
                            "variant_id": item.variant_id,
                            "name": item.name,
                            "quantity": item.quantity,
                        }
                        for i, item in enumerate(state.cart_items)
                    ],
                },
                response_metadata={
                    "domain": "commerce",
                    "clear_pending_action": True,
                    "used_tray": False,
                },
            )
        else:
            removal_result = AgentResult(
                reply_text="",
                intent="commerce",
                safety_reason="cart_item_removal_not_found",
                commercial_data={
                    "removal_requested": True,
                    "removal_supported": True,
                    "mutation_success": False,
                    "reason": resolution_reason,
                    "cart_items": [
                        {
                            "position": i + 1,
                            "product_id": item.product_id,
                            "variant_id": item.variant_id,
                            "name": item.name,
                            "quantity": item.quantity,
                        }
                        for i, item in enumerate(state.cart_items)
                    ],
                },
                response_metadata={
                    "domain": "commerce",
                    "clear_pending_action": True,
                    "used_tray": False,
                },
            )
        return await sales._respond_to_commerce_service(
            message=message,
            plan=plan,
            result=removal_result,
            interpretation=interpretation,
        )
    new_state, rebuild_result = await sales.rebuild_cart_without(
        state, targets, sales.execute_tool
    )
    if rebuild_result.get("success"):
        final_state = new_state
        removal_result = AgentResult(
            reply_text="",
            intent="commerce",
            commercial_data={
                "removal_requested": True,
                "removal_supported": True,
                "mutation_success": True,
                "cart_items": [
                    {
                        "product_id": item.product_id,
                        "variant_id": item.variant_id,
                        "quantity": item.quantity,
                    }
                    for item in final_state.cart_items
                ],
                "shipping_reset": True,
                "payment_reset": True,
            },
            response_metadata={
                "domain": "commerce",
                "cart_state": {
                    "cart_session_id": final_state.cart_session_id,
                    "cart_items": [item.model_dump(mode="json") for item in final_state.cart_items],
                } if final_state.cart_session_id else {"cart_session_id": None, "cart_items": []},
                "purchase_stage": "shopping",
                "clear_pending_action": True,
                "used_tray": True,
            },
        )
    else:
        reason = rebuild_result.get("reason", "unknown")
        is_partial = rebuild_result.get("partial_rebuild", False)
        if reason == "item_not_found":
            safety_reason = "cart_item_removal_item_not_found"
        elif is_partial:
            safety_reason = "cart_item_removal_partial"
        elif reason == "delete_failed":
            safety_reason = "cart_item_removal_delete_failed"
        else:
            safety_reason = "cart_item_removal_failed"
        removal_result = AgentResult(
            reply_text="",
            intent="commerce",
            safety_reason=safety_reason,
            commercial_data={
                "removal_requested": True,
                "removal_supported": True,
                "mutation_success": False,
                "reason": reason,
                "cart_items": [
                    {
                        "product_id": item.product_id,
                        "variant_id": item.variant_id,
                        "quantity": item.quantity,
                    }
                    for item in (new_state.cart_items if is_partial else state.cart_items)
                ] if is_partial or rebuild_result.get("success") is False else [],
            },
            response_metadata={
                "domain": "commerce",
                "clear_pending_action": True,
                "used_tray": True,
            },
        )
    return await sales._respond_to_commerce_service(
        message=message,
        plan=plan,
        result=removal_result,
        interpretation=interpretation,
    )


async def try_catalog_media(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    purchase_action: str | None,
    resolved_product: Any,
    pending_link_requested: bool,
) -> AgentResult | None:
    sales = _sales()
    if interpretation is not None and interpretation.image_request:
        listed_refs = [
            CommerceProductReference.model_validate(
                item.model_dump(exclude={"position"})
            )
            for item in state.last_presented_products[:3]
        ]
        send_listed = False
        if interpretation.reference_position is not None:
            positioned = [
                CommerceProductReference.model_validate(
                    item.model_dump(exclude={"position"})
                )
                for item in state.last_presented_products
                if item.position == interpretation.reference_position
            ]
            if positioned:
                listed_refs = positioned
                send_listed = True
        elif listed_refs and not (message.image_url or "").strip() and (
            sales.wants_all_listed_product_images(message.text)
            or resolved_product is None
        ):
            send_listed = True
        print("[sales.action.guard]", {
            "action": "show_images",
            "target_count": (
                len(listed_refs)
                if send_listed
                else (1 if resolved_product is not None else 0)
            ),
            "allowed": send_listed or resolved_product is not None,
            "blocking_reason": (
                None
                if send_listed or resolved_product is not None
                else "product_target_missing"
            ),
            "inbound_image": bool((message.image_url or "").strip()),
        })
        if send_listed:
            media_result = await sales.resolve_presented_product_images(
                product_references=listed_refs,
                execute=sales.execute_tool,
            )
            if _official_catalog_image_url(media_result):
                return sales._mark_sales_result(
                    media_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source="deterministic_fallback",
                    used_openai_responder=False,
                    used_tray=True,
                    fallback_reason=media_result.safety_reason,
                )
            final = await sales._sales_response_with_openai(
                message,
                plan,
                media_result,
                interpretation,
            )
            if final:
                return final
            return sales._mark_sales_result(
                media_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if media_result.safety_reason == "product_media_technical_failure"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=media_result.safety_reason,
            )
        if resolved_product is None:
            from app.catalog.vision.image_product_id import (
                handle_image_product_search,
                image_search_eligible,
            )

            if image_search_eligible(message):
                image_result = await handle_image_product_search(message)
                if image_result is not None:
                    return sales._mark_sales_result(
                        image_result,
                        interpretation=interpretation,
                        goal="find",
                        response_source=image_result.response_metadata.get(
                            "response_source",
                            "image_vision",
                        ),
                        used_openai_responder=bool(
                            image_result.response_metadata.get("used_openai_responder")
                        ),
                        used_tray=bool(
                            image_result.response_metadata.get("used_tray")
                            or (image_result.commercial_data or {}).get("products")
                        ),
                        fallback_reason=image_result.safety_reason,
                    )
            return sales._mark_sales_result(
                AgentResult(
                    reply_text=(
                        "Pode me enviar a foto do relógio (ou a marca e o modelo) "
                        "que eu identifico no catálogo pra você?"
                        if not (message.image_url or "").strip()
                        else (
                            "Recebi a foto, mas não consegui identificar o produto agora. "
                            "Pode me dizer a marca e o modelo, ou enviar uma imagem mais nítida?"
                        )
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="product_context_missing",
                ),
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
            )
        media_result = await sales.resolve_product_image(
            product_reference=resolved_product,
            execute=sales.execute_tool,
        )
        if _official_catalog_image_url(media_result):
            return sales._mark_sales_result(
                media_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=media_result.safety_reason,
            )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            media_result,
            interpretation,
        )
        if final:
            return final
        return sales._mark_sales_result(
            media_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if media_result.safety_reason == "product_media_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=media_result.safety_reason,
        )
    if interpretation is not None and pending_link_requested:
        if resolved_product is None:
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
        link_facts = await sales.execute_tool(
            "get_product_link",
            {"product_id": resolved_product.product_id},
        )
        link_failed = "error" in link_facts
        product_url = link_facts.get("product_url")
        url_dead = bool(link_facts.get("product_url_dead")) or not isinstance(
            product_url, str
        )
        if url_dead and not link_failed:
            reply_text = _dead_product_link_reply(
                resolved_product=resolved_product,
                link_facts=link_facts,
            )
            safety = "product_link_not_available"
        elif link_failed:
            reply_text = "Não consegui consultar o link oficial deste produto agora."
            safety = "tray_adapter_unavailable"
        else:
            reply_text = f"Link oficial consultado.\n{product_url}"
            safety = None
        link_result = AgentResult(
            reply_text=reply_text,
            intent="commerce",
            handoff_required=False,
            safety_reason=safety,
            commercial_data={
                "product_link": {
                    "product_id": link_facts.get("product_id")
                    or resolved_product.product_id,
                    "product_name": link_facts.get("product_name")
                    or resolved_product.name,
                    "product_url": product_url,
                    "product_url_repaired": bool(
                        link_facts.get("product_url_repaired")
                    ),
                    "product_url_dead": url_dead,
                }
            },
            response_metadata={
                "domain": "commerce",
                "active_product": resolved_product.model_dump(mode="json"),
                "clear_pending_action": True,
                "used_tray": True,
            },
        )
        if url_dead or link_failed:
            return sales._mark_sales_result(
                link_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if safety == "tray_adapter_unavailable"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=True,
                fallback_reason=link_result.safety_reason,
            )
        final = await sales._sales_response_with_openai(
            message,
            plan,
            link_result,
            interpretation,
        )
        if final:
            return final
        return sales._mark_sales_result(
            link_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if link_result.safety_reason == "tray_adapter_unavailable"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=link_result.safety_reason,
        )
    if purchase_action != "set_cart_item_quantity":
        return None
    if resolved_product is None:
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
    if interpretation is None or interpretation.quantity is None:
        quantity_result = AgentResult(
            reply_text="A quantidade final não foi identificada.",
            intent="commerce",
            handoff_required=False,
            safety_reason="cart_validation_error",
            commercial_data={
                "cart": {"mutation_success": False},
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )
    else:
        quantity_result = await sales.set_cart_item_quantity(
            product_reference=resolved_product,
            quantity=interpretation.quantity,
            state=state,
            execute=sales.execute_tool,
        )
    if (
        quantity_result.response_metadata.get("cart_materially_changed") is True
        and state.checkout_channel_preference == "whatsapp"
    ):
        updated_state = sales.evolve_commerce_state(state, quantity_result)
        known_zipcode = updated_state.checkout_draft.address.zip_code
        if known_zipcode:
            quote_result = await sales.quote_shipping(
                state=updated_state,
                zipcode=known_zipcode,
                execute=sales.execute_tool,
            )
            quantity_result = sales._combine_checkout_and_followup_results(
                quantity_result,
                quote_result,
            )
    final = await sales._sales_response_with_openai(
        message,
        plan,
        quantity_result,
        interpretation,
    )
    if final:
        return final
    return sales._mark_sales_result(
        quantity_result,
        interpretation=interpretation,
        goal=plan.get("goal"),
        response_source="deterministic_fallback",
        used_openai_responder=False,
        used_tray=bool(quantity_result.response_metadata.get("used_tray")),
        fallback_reason=quantity_result.safety_reason,
    )
