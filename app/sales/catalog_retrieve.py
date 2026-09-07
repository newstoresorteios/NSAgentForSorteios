"""Retrieve, clarify, talk-first, and leftover Tray catalog replies.

Look up patched names on ``app.sales_agent`` at call time.
"""

from __future__ import annotations

import re
from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, IncomingMessage, SalesInterpretation


def _sales():
    import app.sales_agent as sales_mod

    return sales_mod


async def retrieve_catalog_or_clarify(
    *,
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    interpretation: SalesInterpretation | None,
    plan: dict[str, Any],
    state: CommerceConversationState,
    recent_turns: list[dict[str, Any]] | None,
    resolved_product: Any,
) -> AgentResult | None:
    sales = _sales()
    from .intent_router import route_sales_intent

    intent_route = route_sales_intent(
        interpretation=interpretation,
        plan=plan,
        message_text=message.text,
        commerce_state=state,
        recent_turns=recent_turns,
    )
    discovery_state = intent_route.discovery_state
    if intent_route.browse_reset and state is not None:
        from .dialogue_phase import reset_browse_memory_keep_orders

        state = reset_browse_memory_keep_orders(state)
    if intent_route.plan_intent and intent_route.plan_intent != plan.get("intent"):
        plan = {**plan, "intent": intent_route.plan_intent}
    print("[sales.agent] planner", {
        "source": plan.get("_source", "fallback"),
        "action": plan.get("intent"),
        "route_kind": intent_route.route_kind,
        "has_query": bool(plan.get("query")),
        "has_brand": bool((plan.get("filters") or {}).get("brand")),
        "has_model": bool((plan.get("filters") or {}).get("model")),
    })
    if discovery_state:
        print("[sales.discovery]", {
            "clarification_count": discovery_state["clarification_count"],
            "enough_information_to_search": discovery_state["enough_information_to_search"],
            "ready_for_retrieval": discovery_state["ready_for_retrieval"],
            "stop_clarification": discovery_state["stop_clarification"],
            "known_preferences_count": discovery_state["known_preferences_count"],
        })
    vague_query = intent_route.vague_query
    if interpretation and discovery_state and intent_route.needs_clarification_before_retrieval:
        return await sales.generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    if interpretation and discovery_state and intent_route.vague_query_clarification:
        return await sales.generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    def _close_hold_result() -> AgentResult:
        try:
            from app.ops.observability import record_close_miss

            record_close_miss(
                reason="purchase_close_hold",
                channel=message.channel,
                dialogue_phase=(state.dialogue_phase if state else None),
            )
        except Exception as exc:
            from app.sales import log_swallowed

            log_swallowed("handle.record_close_miss", exc)
        return sales._mark_sales_result(
            AgentResult(
                reply_text=sales._purchase_close_hold_reply(
                    message=message,
                    state=state,
                    interpretation=interpretation,
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="purchase_close_hold",
                response_metadata={"domain": "commerce"},
            ),
            interpretation=interpretation,
            goal=(interpretation.goal if interpretation else "buy"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_close_hold",
        )

    if plan.get("intent") == "clarification" or vague_query:
        if intent_route.purchase_close_hold:
            return _close_hold_result()
        clarification = str(plan.get("clarification_question") or "").strip()
        if not clarification and interpretation is not None:
            clarification = (
                sales._persona_qualification_question(
                    interpretation,
                    discovery_state,
                )
                or str(interpretation.clarification_question or "").strip()
            )
        if not clarification:
            return await sales.generate_clarification_reply(
                message=message,
                interpretation=interpretation
                or SalesInterpretation(
                    domain="commerce",
                    goal="discover",
                    needs_clarification=True,
                ),
                recent_turns=recent_turns,
                discovery_state=discovery_state,
            )
        result = AgentResult(
            reply_text=clarification,
            intent="commerce",
            handoff_required=False,
            safety_reason="commerce_clarification",
        )
        return sales._mark_sales_result(
            result,
            interpretation=None,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
        )

    action = {
        "product_search": "product_search",
        "recommendation": "product_search",
        "product_comparison": "product_search",
        "price": "product_price",
        "inventory": "product_inventory",
        "coupon": "coupon_search",
    }.get(str(plan.get("intent")))
    if not action:
        return None

    if interpretation is not None and resolved_product is not None:
        tray_result = await sales._execute_contextual_product_lookup(
            interpretation,
            resolved_product,
        )
    elif intent_route.blocks_compiled_product_retrieval(
        resolved_product,
        interpretation,
    ):
        print("[sales.agent] compiled_skipped", {
            "route_kind": intent_route.route_kind,
            "purchase_close": intent_route.purchase_close,
            "action": action,
        })
        return _close_hold_result()
    elif intent_route.skip_catalog_fanout:
        tray_result = sales._session_product_facts_result(state, resolved_product)
        print("[sales.agent] talk_first", {
            "skip_catalog_fanout": True,
            "answer_strategy": getattr(
                getattr(interpretation, "_turn_understanding", None),
                "answer_strategy",
                None,
            ),
            "goal": interpretation.goal if interpretation is not None else None,
            "session_products": len((tray_result.commercial_data or {}).get("products") or []),
        })
    elif interpretation is not None and action in {
        "product_search",
        "product_price",
        "product_inventory",
    }:
        tray_result = await sales._execute_compiled_product_retrieval(
            interpretation,
            message_text=message.text,
            commerce_state=state,
        )
    else:
        leftover_blocked = bool(
            intent_route.skip_catalog_fanout
            or intent_route.purchase_close
            or intent_route.purchase_close_hold
            or intent_route.route_kind == "close"
        )
        if leftover_blocked:
            print("[sales.agent] leftover_skipped", {
                "purchase_close": intent_route.purchase_close,
                "purchase_close_hold": intent_route.purchase_close_hold,
                "skip_catalog_fanout": intent_route.skip_catalog_fanout,
            })
            if (
                intent_route.purchase_close
                or intent_route.purchase_close_hold
                or intent_route.route_kind == "close"
            ):
                return _close_hold_result()
            tray_result = sales._session_product_facts_result(state, resolved_product)
        else:
            queries = [str(plan.get("query") or "").strip()]
            code_value = re.sub(r"^(?:ean|sku|ref(?:er[êe]ncia)?)\s+", "", queries[0], flags=re.IGNORECASE)
            code_query = bool(re.fullmatch(r"[A-Za-z0-9._/-]+", code_value)) and any(char.isdigit() for char in code_value)
            subject = plan.get("subject") or {}
            if action == "product_search" and not code_query:
                model = str(subject.get("model") or "").strip()
                brand = str(subject.get("brand") or "").strip()
                if model:
                    queries.append(model)
                if brand:
                    queries.append(brand)
            queries = list(dict.fromkeys(query for query in queries if query or action == "coupon_search"))
            tray_result = None
            last_raw_result = None
            for attempt, query in enumerate(queries[:3], start=1):
                attempt_plan = {**plan, "query": query, "subject": {**(plan.get("subject") or {}), "query": query}}
                print("[sales.agent] tray_request", {"capability": action, "attempt": attempt, "strategy": "initial" if attempt == 1 else "progressive"})
                raw_result = await sales.handle_commerce_message(
                    message,
                    facts,
                    customer_context,
                    action=action,
                    query=query,
                )
                last_raw_result = raw_result
                print("[sales.agent] tray_result", {"ok": raw_result is not None and raw_result.safety_reason != "tray_adapter_unavailable", "results_count": len((raw_result.commercial_data or {}).get("products", [])) if raw_result else 0})
                tray_result = sales._ranked_result(raw_result, attempt_plan) if raw_result else None
                if tray_result:
                    print("[sales.agent] ranking", {"input_count": len((raw_result.commercial_data or {}).get("products", [])), "output_count": len((tray_result.commercial_data or {}).get("products", []))})
                    break
                if raw_result and raw_result.safety_reason == "tray_adapter_unavailable":
                    tray_result = raw_result
                    break
                if raw_result and raw_result.safety_reason not in {"product_not_found", "ambiguous_product"}:
                    tray_result = raw_result
                    break
            if tray_result is None:
                tray_result = last_raw_result
    if tray_result is None:
        return None
    if interpretation is not None:
        phase_hint: dict[str, Any] = {}
        try:
            from .dialogue_phase import metadata_dialogue_phase_hint

            phase_hint = metadata_dialogue_phase_hint(
                interpretation=interpretation,
                message_text=message.text,
                presented_products=bool(
                    (tray_result.response_metadata or {}).get("presented_products")
                ),
            )
        except Exception:
            phase_hint = {}
        prefs_dump = interpretation.preferences.model_dump(
            mode="json",
            exclude_none=True,
        )
        try:
            from .turn_contract import next_locked_identity

            identity = next_locked_identity(interpretation, state, message.text)
            if identity:
                prefs_dump = {**prefs_dump, "locked_identity": identity}
            excluded = list(getattr(interpretation, "_excluded_product_ids", None) or [])
            prior = (state.active_preferences or {}).get("excluded_product_ids") or []
            merged = list(dict.fromkeys([*[str(item) for item in prior if item], *excluded]))
            if merged:
                prefs_dump = {**prefs_dump, "excluded_product_ids": merged}
        except Exception as exc:
            from app.sales import log_swallowed

            log_swallowed("handle.locked_identity", exc)
        try:
            from .qualification_slots import attach_qualification_slots

            prefs_dump = attach_qualification_slots(
                prefs_dump,
                interpretation,
                prior=state.active_preferences,
            )
        except Exception as exc:
            from app.sales import log_swallowed

            log_swallowed("handle.qual_slots", exc)
        tray_result.response_metadata.update({
            "active_topic": interpretation.active_topic,
            "purchase_stage": interpretation.purchase_stage,
            "active_preferences": prefs_dump,
            **phase_hint,
        })
        if resolved_product is not None:
            tray_result.response_metadata["active_product"] = resolved_product.model_dump(mode="json")
    if (
        plan.get("intent") in {"purchase_intent", "recommendation", "clarification"}
        and tray_result.safety_reason == "product_not_found"
        and not (discovery_state and discovery_state["force_retrieval"])
    ):
        if interpretation:
            return await sales.generate_clarification_reply(
                message=message,
                interpretation=interpretation,
                recent_turns=recent_turns,
                context_note="A busca atual não trouxe candidatos confiáveis; peça um critério diferente sem afirmar que o produto não existe.",
                used_tray=True,
                discovery_state=discovery_state,
            )
    final = await sales._sales_response_with_openai(
        message,
        plan,
        tray_result,
        interpretation,
        sales.evolve_commerce_state(state, tray_result),
    )
    print("[sales.agent] responder", {"source": "openai" if final else "deterministic_fallback"})
    if final:
        return final
    if interpretation is not None and sales._comparison_needs_qualification(interpretation):
        return await sales.generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    technical_failure = tray_result.safety_reason in {
        "tray_adapter_unavailable",
        "product_match_failed",
    }
    response_source = "technical_fallback" if technical_failure else "deterministic_fallback"
    return sales._mark_sales_result(
        tray_result,
        interpretation=interpretation,
        goal=plan.get("goal"),
        response_source=response_source,
        used_openai_responder=False,
        used_tray=True,
        fallback_reason=(
            tray_result.safety_reason
            if response_source == "technical_fallback"
            else "sales_responder_unavailable"
        ),
    )
