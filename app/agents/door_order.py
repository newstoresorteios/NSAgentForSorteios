"""Deterministic order / PIX / presented-catalog resumes for the door."""

from __future__ import annotations

from typing import Any

from app.models import AgentResult, IncomingMessage


def _door():
    import app.agents.door as door_mod

    return door_mod


async def try_tax_document_route(
    message: IncomingMessage,
    commerce_state: Any,
) -> AgentResult | None:
    door = _door()
    if getattr(commerce_state, "pending_action", None) != "awaiting_order_customer_document":
        return None
    customer_document = door.extract_valid_tax_document(message.text)
    if customer_document:
        document_kind, document = customer_document
        result = await door.find_order_by_customer_document(
            state=commerce_state,
            execute=door.execute_tool,
            document_kind=document_kind,
            document=document,
        )
        return door._annotate_agent_result(
            result,
            domain="commerce",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=bool(result.response_metadata.get("used_tray")),
        )
    if door.contains_tax_document_candidate(message.text):
        result = door.invalid_tax_document_result()
        return door._annotate_agent_result(
            result,
            domain="commerce",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    return None


async def try_order_resume_route(
    *,
    message: IncomingMessage,
    commerce_state: Any,
    context_handles: dict[str, Any],
    fresh_start: bool,
    soft_greeting: bool,
    resume_pending_order_early: bool,
    order_reference: Any,
) -> AgentResult | None:
    door = _door()
    stored_payment = door.build_pending_payment_resume_result(commerce_state)
    if stored_payment is not None and (
        door.is_payment_link_request(message.text) or resume_pending_order_early
    ):
        order_label = commerce_state.order_id or commerce_state.order_lookup_id
        print("[sales.order.route]", {
            "route": "transcript_payment_url",
            "order_id_present": bool(order_label),
            "payment_url_present": True,
            "resume_pending_order": resume_pending_order_early,
        })
        return door._annotate_agent_result(
            stored_payment,
            domain="commerce",
            response_source="context_resume_payment_url",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    if (not fresh_start) and door.should_redisplay_presented_catalog(
        message.text, commerce_state
    ):
        presented = door.build_presented_catalog_resume_result(commerce_state)
        if presented is not None:
            return door._annotate_agent_result(
                presented,
                domain="commerce",
                response_source="context_resume_presented_catalog",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
            )
    if door.is_payment_link_request(message.text) and commerce_state.order_payment_url:
        order_label = commerce_state.order_id or commerce_state.order_lookup_id
        reply = (
            f"Seu pedido {order_label} ainda está aguardando pagamento. "
            f"Segue o link: {commerce_state.order_payment_url}"
            if order_label
            else (
                "Seu pedido ainda está aguardando pagamento. "
                f"Segue o link: {commerce_state.order_payment_url}"
            )
        )
        print("[sales.order.route]", {
            "route": "transcript_payment_url",
            "order_id_present": bool(order_label),
            "payment_url_present": True,
        })
        return door._annotate_agent_result(
            AgentResult(
                reply_text=reply,
                intent="commerce",
                commercial_data={
                    "order_id": order_label,
                    "payment": {
                        "payment_url": commerce_state.order_payment_url,
                        "status": commerce_state.order_payment_status or "awaiting_payment",
                    },
                },
                response_metadata={
                    "domain": "commerce",
                    "pending_action": "awaiting_payment",
                    "order_state": {"order_id": order_label} if order_label else {},
                    "payment_state": {
                        "order_payment_url": commerce_state.order_payment_url,
                        "order_payment_status": (
                            commerce_state.order_payment_status or "awaiting_payment"
                        ),
                    },
                    "used_tray": False,
                },
            ),
            domain="commerce",
            response_source="context_resume_payment_url",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    if door.is_order_notes_request(message.text, commerce_state=commerce_state):
        result = door.order_notes_unavailable_result(commerce_state)
        return door._annotate_agent_result(
            result,
            domain="commerce",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    wants_order_context = (
        door.is_order_lookup_request(message.text, commerce_state=commerce_state)
        or door.is_payment_link_request(message.text)
        or door.is_unpaid_order_resume_request(message.text)
    )
    known_order_tokens = [
        token
        for token in (
            order_reference,
            commerce_state.order_id,
            commerce_state.order_lookup_id,
            *(context_handles.get("order_ids") or []),
        )
        if token
    ]
    has_numeric_order_id = any(str(token).isdigit() for token in known_order_tokens)
    if wants_order_context and (
        not (
            order_reference
            or commerce_state.order_id
            or commerce_state.order_lookup_id
            or commerce_state.order_session_id
            or commerce_state.cart_session_id
            or commerce_state.order_payment_url
        )
        or not has_numeric_order_id
    ):
        recovered_order_id = await door.recover_order_id_from_customer(
            execute=door.execute_tool,
            handles=context_handles,
            preferred_codes=[str(token) for token in known_order_tokens],
        )
        if recovered_order_id:
            commerce_state.order_id = recovered_order_id
            commerce_state.order_lookup_id = (
                commerce_state.order_lookup_id or recovered_order_id
            )
            if commerce_state.pending_action is None:
                commerce_state.pending_action = "awaiting_payment"
    resume_pending_order = door.should_resume_pending_order(
        message.text,
        commerce_state,
        is_greeting=soft_greeting,
        allow_without_state=bool(
            context_handles.get("order_ids")
            or context_handles.get("payment_urls")
            or context_handles.get("documents")
            or context_handles.get("emails")
        ),
    )
    if (
        door.is_order_lookup_request(message.text, commerce_state=commerce_state)
        or resume_pending_order
        or door.is_payment_link_request(message.text)
    ) and (
        order_reference
        or commerce_state.order_id
        or commerce_state.order_lookup_id
        or commerce_state.order_session_id
        or commerce_state.cart_session_id
        or commerce_state.order_payment_url
    ):
        print("[sales.order.route]", {
            "route": (
                "context_resume_payment"
                if (
                    resume_pending_order or door.is_payment_link_request(message.text)
                )
                and not door.is_order_lookup_request(
                    message.text, commerce_state=commerce_state
                )
                else "deterministic_status_lookup"
            ),
            "order_reference_present": bool(order_reference),
            "state_order_present": bool(commerce_state.order_id),
            "resume_pending_order": resume_pending_order,
            "recovered_from_transcript": bool(context_handles.get("order_ids")),
            "customer_handles": {
                "emails": len(context_handles.get("emails") or []),
                "documents": len(context_handles.get("documents") or []),
            },
        })
        if (
            (
                resume_pending_order
                or door.is_payment_link_request(message.text)
            )
            and commerce_state.order_id
            and (
                commerce_state.pending_action == "awaiting_payment"
                or commerce_state.order_payment_url
                or door.is_payment_link_request(message.text)
                or door.is_unpaid_order_resume_request(message.text)
            )
            and not door.is_order_lookup_request(
                message.text, commerce_state=commerce_state
            )
        ):
            result = await door.inspect_order_payment(
                state=commerce_state,
                execute=door.execute_tool,
                order_id=commerce_state.order_id,
            )
            if not (result.commercial_data or {}).get("payment", {}).get("payment_url"):
                if commerce_state.order_payment_url:
                    result = AgentResult(
                        reply_text=(
                            f"Seu pedido {commerce_state.order_id} ainda está aguardando "
                            f"pagamento. Segue o link: {commerce_state.order_payment_url}"
                        ),
                        intent="commerce",
                        commercial_data={
                            "order_id": commerce_state.order_id,
                            "payment": {
                                "payment_url": commerce_state.order_payment_url,
                                "status": commerce_state.order_payment_status,
                            },
                        },
                        response_metadata={
                            "domain": "commerce",
                            "pending_action": "awaiting_payment",
                            "order_state": {"order_id": commerce_state.order_id},
                            "payment_state": {
                                "order_payment_url": commerce_state.order_payment_url,
                                "order_payment_status": (
                                    commerce_state.order_payment_status
                                ),
                            },
                        },
                    )
        else:
            result = await door.get_order_facts(
                state=commerce_state,
                execute=door.execute_tool,
                order_id=order_reference,
            )
            if (
                door.is_unpaid_order_resume_request(message.text)
                and commerce_state.order_id
                and not (result.commercial_data or {}).get("payment")
            ):
                payment_result = await door.inspect_order_payment(
                    state=commerce_state,
                    execute=door.execute_tool,
                    order_id=commerce_state.order_id,
                )
                if (payment_result.commercial_data or {}).get("payment"):
                    result = payment_result
        return door._annotate_agent_result(
            result,
            domain="commerce",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=bool(result.response_metadata.get("used_tray")),
        )
    return None
