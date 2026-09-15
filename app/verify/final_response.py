"""Validate the actual outbound text and derive memory from delivered options."""
from __future__ import annotations

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.specs.requirements import normalize_requirements, feature_evidence
from app.commerce.commerce_context import evolve_commerce_state
from app.models import SalesInterpretation


def result_interpretation(result):
    raw = (result.response_metadata or {}).get("interpretation")
    try:
        return SalesInterpretation.model_validate(raw) if isinstance(raw, dict) else None
    except ValueError:
        return None


def grounded_catalog_fallback(result):
    """Rebuild from live facts; an old fallback string is not evidence."""
    interpretation = result_interpretation(result)
    if interpretation is None or interpretation.goal not in {"find", "recommend", "discover"}:
        return None
    products = list((result.commercial_data or {}).get("products") or [])
    if not products or not all(p.get("_revalidated") for p in products):
        return None
    validation = (result.response_metadata or {}).get("factual_validation") or {}
    if validation.get("violations") or validation.get("valid") is False:
        return None
    kept = hard_filter_products(products, interpretation, mode="recommendation")
    if len(kept) != len(products):
        return None
    from app.catalog.retrieval.technical import confirmed_product_reply
    from app.commerce.commerce_router import _product_result
    fixed = result.model_copy(deep=True)
    fixed.reply_text = confirmed_product_reply(kept, interpretation) if normalize_requirements(interpretation) else _product_result("product_search", kept).reply_text
    fixed.response_metadata.update(response_source="grounded_fallback", used_openai_responder=False)
    return fixed


def finalize_response(result, *, incoming, interpretation, previous_state):
    from app.sales.responder import recommendation_identifies_candidate
    from app.catalog.retrieval.technical import technical_miss
    from app.sales.answer_council import build_turn_contract, check_pedido, check_fatos

    metadata = result.response_metadata
    interpretation = interpretation or result_interpretation(result)
    issues = list((metadata.get("final_response_validation") or {}).get("rejected_issues") or [])
    products = [p for p in (result.commercial_data or {}).get("products", []) if isinstance(p, dict)]
    requirements = normalize_requirements(interpretation, incoming.text) if interpretation else {}
    if requirements and products and not result.handoff_required:
        evidence = [feature_evidence(p, requirements) for p in products]
        if any(e["status"] != "matched" for e in evidence) or len(hard_filter_products(products, interpretation, mode="recommendation")) != len(products):
            issues.append("final_technical_requirements_failed")
            replacement = technical_miss(interpretation, unknown=any(e["status"] == "unknown" for e in evidence), evidence=evidence)
            metadata.update(replacement.response_metadata)
            result.reply_text = replacement.reply_text
            result.safety_reason = replacement.safety_reason
            result.commercial_data = replacement.commercial_data
            products = []
    if not result.handoff_required:
        contract = build_turn_contract(message_text=incoming.text, interpretation=interpretation, commerce_state=previous_state)
        checks = [check_pedido(result, contract), check_fatos(result, contract)]
        violations = list(dict.fromkeys(issue for check in checks for issue in check.issues))
        if violations:
            from app.sales.answer_council import _honest_constraint_reply
            issues.extend(violations)
            result = _honest_constraint_reply(result, contract, interpretation)
            metadata = result.response_metadata
            products = [p for p in (result.commercial_data or {}).get("products", []) if isinstance(p, dict)]
    # A repair is another response: check it before marking the final text valid.
    remaining = []
    if not result.handoff_required:
        remaining = list(dict.fromkeys(issue for check in (check_pedido(result, contract), check_fatos(result, contract)) for issue in check.issues))
        if remaining:
            from app.configuration.runtime import message
            result.reply_text = message("critique_handoff")
            result.handoff_required = True
            result.safety_reason = "final_response_validation_failed"
    delivered = [] if result.handoff_required else [p for p in products if recommendation_identifies_candidate(result.reply_text, [p])]
    # If a later review removed every option, do not keep the generated shortlist.
    clear = result.handoff_required or bool(metadata.get("clear_presented_products")) or (bool(products) and not delivered)
    if clear:
        result.commercial_data = {**(result.commercial_data or {}), "products":[]}
        metadata.update(presented_products=False, clear_presented_products=True, clear_active_product=True)
        metadata.pop("active_product", None)
        metadata.pop("activate_first_product", None)
        metadata["allowed_id_sets"] = {"allowed_product_ids":[], "allowed_variant_ids":[], "allowed_catalog_item_keys":[]}
        metadata["product_resolution_state"] = "handoff" if result.handoff_required else metadata.get("product_resolution_state", "not_presented")
        if metadata["product_resolution_state"] in {"options_presented", "plausible_matches"}:
            metadata["product_resolution_state"] = "not_presented"
        metadata["dialogue_phase"] = "discovery"
    elif delivered:
        result.commercial_data = {**(result.commercial_data or {}), "products":delivered}
        metadata["presented_products"] = True
        from app.catalog.index.catalog_index import build_allowed_id_sets
        metadata["allowed_id_sets"] = {k:sorted(v) for k,v in build_allowed_id_sets(delivered).items()}
    if interpretation:
        metadata.setdefault("domain", interpretation.domain)
        metadata["active_preferences"] = {**metadata.get("active_preferences", {}), **interpretation.preferences.model_dump(mode="json", exclude_none=True)}
    result.response_metadata = metadata
    state = evolve_commerce_state(previous_state, result)
    if clear:
        state.last_presented_products = []
        state.forget_shortlist = True
        if not state.cart_session_id and not state.order_id:
            state.active_product = None
            state.pending_action = None
            state.pending_action_product_ids = []
            state.dialogue_phase = "discovery"
    metadata["final_response_validation"] = {"passed":not remaining, "corrected":bool(issues), "rejected_issues":issues,
                                               "remaining_issues":remaining,
                                               "delivered_product_ids":[str(p.get("id")) for p in delivered] if not clear else []}
    return result, state
