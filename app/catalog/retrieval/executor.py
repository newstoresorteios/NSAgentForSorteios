"""Compiled product retrieval orchestration. Phases live in sibling modules."""

from __future__ import annotations

from typing import Any

from app.catalog.category.resolver import CategoryResolver
from app.catalog.retrieval.availability import (
    commercial_availability_facts,
    product_availability_state,
)
from app.catalog.retrieval.compiler import ProductRetrievalCompiler
from app.catalog.retrieval.harvest import harvest_family_and_color, merge_brand_cache
from app.catalog.retrieval.limits import ToolExecutor
from app.catalog.retrieval.near_match import (
    handle_empty_candidates,
    handle_hard_filter_miss,
    last_chance_from_message_text,
)
from app.catalog.retrieval.ports import (
    BudgetHardMiss,
    ListQueryExtras,
    RequiresTrayRefresh,
    default_budget_hard_miss,
    default_list_query_extras,
    default_requires_tray_refresh,
    resolve_execute_tool,
)
from app.catalog.retrieval.present import present_compiled_results
from app.catalog.retrieval.probes import run_discovery, run_probes, seed_from_catalog_index
from app.catalog.retrieval.scoring import (
    prefilter_specific_candidates,
    score_catalog_candidates,
    semantic_preferences,
    specific_product_search_terms,
)
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.specific import match_specific_products
from app.catalog.retrieval.tokens import preference_color_tokens
from app.catalog.retrieval.types import ProductMatchError
from app.catalog.retrieval.variants import enrich_product_variants
from app.commerce.commerce_context import CommerceProductReference
from app.commerce.commerce_router import _product_result
from app.models import AgentResult, SalesInterpretation


async def execute_contextual_product_lookup(
    interpretation: SalesInterpretation,
    product_reference: CommerceProductReference,
    *,
    execute_tool: ToolExecutor | None = None,
) -> AgentResult:
    tool = resolve_execute_tool(execute_tool)
    product_id = product_reference.product_id
    print("[sales.product.resolve]", {
        "strategy": "context",
        "has_brand": bool(product_reference.brand),
        "has_model": False,
        "candidate_count": 1,
        "matched_count": 1,
    })
    current = await tool("get_product", {"product_id": product_id})
    if "error" in current:
        return AgentResult(
            reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
            intent="commerce",
            handoff_required=False,
            safety_reason="tray_adapter_unavailable",
        )
    product = {
        key: value
        for key, value in {
            "id": product_id,
            "name": product_reference.name,
            "reference": product_reference.reference,
            "ean": product_reference.ean,
            "brand": product_reference.brand,
        }.items()
        if value is not None
    }
    product.update(current)
    inventory: dict[str, Any] | None = None
    if "inventory" in interpretation.information_needed:
        inventory = await tool("check_inventory", {"product_id": product_id})
        if "error" in inventory:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
    enriched = await enrich_product_variants([product], interpretation, tool)
    availability_input = {
        **enriched[0],
        **(inventory or {}),
    }
    availability_facts = commercial_availability_facts(availability_input)
    enriched[0]["commercial_availability"] = availability_facts
    print("[sales.availability.fact]", {
        "has_stock": availability_facts["has_stock"],
        "has_lead_time": availability_facts["has_lead_time"],
        "immediate_delivery_supported": availability_facts["immediate_delivery_supported"],
        "in_ready_to_ship_category": availability_facts["in_ready_to_ship_category"],
    })
    availability_state = product_availability_state(enriched[0])
    print("[sales.product.availability]", {
        "resolved": True,
        "available_state": availability_state,
    })
    if availability_state == "unavailable":
        return AgentResult(
            reply_text=(
                "Encontrei esse modelo no catálogo, mas ele está indisponível no momento. "
                "Posso procurar outras versões dele ou modelos semelhantes."
            ),
            intent="commerce",
            handoff_required=False,
            safety_reason="product_unavailable",
            commercial_data={
                "products": enriched,
                "availability_state": availability_state,
            },
            response_metadata={
                "active_product": product_reference.model_dump(mode="json"),
                "presented_products": False,
                "product_resolution_state": "found_unavailable",
            },
        )
    result = _product_result("product_search", enriched)
    if inventory is not None:
        result.commercial_data = {
            "products": enriched,
            "inventory": inventory,
        }
    result.response_metadata.update({
        "active_product": product_reference.model_dump(mode="json"),
        "presented_products": False,
        "product_resolution_state": (
            "found_available" if availability_state == "available" else "found_unknown"
        ),
    })
    return result


async def _execute_compiled_product_retrieval_unlocked(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
    execute_tool: ToolExecutor | None = None,
    list_query_extras: ListQueryExtras = default_list_query_extras,
    requires_tray_refresh: RequiresTrayRefresh = default_requires_tray_refresh,
    budget_hard_miss: BudgetHardMiss = default_budget_hard_miss,
) -> AgentResult | None:
    tool = resolve_execute_tool(execute_tool)
    initial_plan = ProductRetrievalCompiler.compile(interpretation)
    category_resolution = None
    if (
        initial_plan.mode == "recommendation"
        and interpretation.subject.product_type
    ) or (
        initial_plan.mode == "exact"
        and interpretation.subject.product_type
        and not interpretation.subject.brand
        and not interpretation.subject.reference
        and not interpretation.subject.ean
    ):
        category_resolution = await CategoryResolver(tool).resolve(
            interpretation.subject.product_type
        )
    retrieval_plan = ProductRetrievalCompiler.compile(
        interpretation,
        category_ids=(category_resolution.product_category_ids if category_resolution else ()),
    )
    preferences = semantic_preferences(interpretation)
    has_budget = any((
        interpretation.preferences.budget_min is not None,
        interpretation.preferences.budget_max is not None,
    ))
    print("[sales.retrieval.plan]", {
        "goal": interpretation.goal,
        "has_product_type": bool(interpretation.subject.product_type),
        "has_brand": bool(interpretation.subject.brand),
        "has_model": bool(interpretation.subject.model),
        "has_budget": has_budget,
        "semantic_preferences_count": len(preferences),
        "candidate_limit": retrieval_plan.candidate_limit,
    })
    if not retrieval_plan.requests:
        return None

    excluded_ids = {
        str(item)
        for item in (getattr(interpretation, "_excluded_product_ids", None) or [])
        if str(item).strip()
    }
    session = RetrievalSession(
        interpretation=interpretation,
        retrieval_plan=retrieval_plan,
        message_text=message_text,
        execute_tool=tool,
        has_budget=has_budget,
        category_resolution=category_resolution,
        excluded_ids=excluded_ids,
        list_query_extras=list_query_extras,
        requires_tray_refresh=requires_tray_refresh,
        budget_hard_miss=budget_hard_miss,
    )
    discovery_strategies = {"brand_candidates", "category_candidates"}
    probe_requests = [
        request
        for request in retrieval_plan.requests
        if not (
            retrieval_plan.mode == "exact"
            and request.strategy in discovery_strategies
        )
    ]
    discovery_requests = [
        request
        for request in retrieval_plan.requests
        if retrieval_plan.mode == "exact"
        and request.strategy in discovery_strategies
    ]
    search_term_count = len(specific_product_search_terms(interpretation))

    await seed_from_catalog_index(session)
    await run_probes(session, probe_requests)
    await run_discovery(
        session,
        discovery_requests,
        search_term_count=search_term_count,
    )
    await harvest_family_and_color(session)
    await merge_brand_cache(session)

    if retrieval_plan.mode == "recommendation" and not discovery_requests:
        session.refresh_hard_filtered()

    if retrieval_plan.mode == "exact" and session.candidates:
        require_color = bool(preference_color_tokens(interpretation))
        scored_hits = score_catalog_candidates(
            session.candidates,
            interpretation,
            require_color=require_color,
            allow_movement_mismatch=False,
            limit=retrieval_plan.candidate_limit,
        )
        matcher_candidates = scored_hits or prefilter_specific_candidates(
            session.candidates,
            interpretation,
            limit=retrieval_plan.candidate_limit,
        )
        print("[sales.catalog.prefilter]", {
            "discovered_count": len(session.candidates),
            "shortlisted_count": len(matcher_candidates),
            "keyword_hit_count": len(scored_hits),
        })
        try:
            session.specific_resolution = await match_specific_products(
                matcher_candidates if scored_hits else session.candidates,
                interpretation,
            )
            session.hard_filtered = list(session.specific_resolution.products)
        except ProductMatchError:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_match_failed",
            )
        print("[sales.product.disambiguation]", {
            "candidate_pool_count": len(session.candidates),
            "plausible_count": len(session.hard_filtered),
            "match_status": session.specific_resolution.status,
            "used_brand_candidates": session.used_brand_candidates,
            "used_category_candidates": session.used_category_candidates,
        })

    empty = await handle_empty_candidates(session)
    if empty is not None:
        return empty

    identity_hit = await last_chance_from_message_text(
        session,
        mode=retrieval_plan.mode,
    )
    if identity_hit is not None:
        return identity_hit

    miss = await handle_hard_filter_miss(session)
    if miss is not None:
        return miss

    return await present_compiled_results(session)


async def execute_compiled_product_retrieval(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
    commerce_state=None,
    execute_tool: ToolExecutor | None = None,
    list_query_extras: ListQueryExtras = default_list_query_extras,
    requires_tray_refresh: RequiresTrayRefresh = default_requires_tray_refresh,
    budget_hard_miss: BudgetHardMiss = default_budget_hard_miss,
) -> AgentResult | None:
    """Run compiled retrieval. Sales facade binds contract/authorization."""
    del commerce_state
    return await _execute_compiled_product_retrieval_unlocked(
        interpretation,
        message_text=message_text,
        execute_tool=execute_tool,
        list_query_extras=list_query_extras,
        requires_tray_refresh=requires_tray_refresh,
        budget_hard_miss=budget_hard_miss,
    )


_execute_contextual_product_lookup = execute_contextual_product_lookup
_execute_compiled_product_retrieval = execute_compiled_product_retrieval
