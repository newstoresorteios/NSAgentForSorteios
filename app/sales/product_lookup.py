"""Compiled / contextual product retrieval execution (IQ-08)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..catalog_cache import ensure_brand_pool_in_candidates
from ..category_resolver import CategoryResolver
from ..commerce_context import CommerceProductReference
from ..config import get_settings
from ..models import AgentResult, SalesInterpretation
from ..product_retrieval import (
    apply_persona_presentation_order,
    commercial_availability_facts,
    ProductMatchError,
    ProductRetrievalCompiler,
    customer_result_limit,
    enrich_product_variants,
    exact_progress_matches,
    hard_filter_products,
    identity_core_tokens,
    infer_family_codes_from_candidates,
    match_specific_products,
    preference_color_search_labels,
    preference_color_tokens,
    prefilter_specific_candidates,
    product_matches_color_tokens,
    product_availability_state,
    revalidate_products,
    rerank_products,
    score_catalog_candidates,
    semantic_preferences,
    soft_confirm_candidates,
    specific_product_search_terms,
)


async def _call_execute_tool(name: str, arguments: dict[str, Any]):
    """Prefer sales_agent.execute_tool so tests can monkeypatch it."""
    from app import sales_agent

    return await sales_agent.execute_tool(name, arguments)


async def execute_contextual_product_lookup(
    interpretation: SalesInterpretation,
    product_reference: CommerceProductReference,
) -> AgentResult:
    product_id = product_reference.product_id
    print("[sales.product.resolve]", {
        "strategy": "context",
        "has_brand": bool(product_reference.brand),
        "has_model": False,
        "candidate_count": 1,
        "matched_count": 1,
    })
    current = await _call_execute_tool("get_product", {"product_id": product_id})
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
        inventory = await _call_execute_tool("check_inventory", {"product_id": product_id})
        if "error" in inventory:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
    enriched = await enrich_product_variants([product], interpretation, _call_execute_tool)
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
    from ..commerce_router import _product_result

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


async def execute_compiled_product_retrieval(
    interpretation: SalesInterpretation,
) -> AgentResult | None:
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
        category_resolution = await CategoryResolver(_call_execute_tool).resolve(
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

    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    hard_filtered: list[dict[str, Any]] = []
    product_lookup_failed = False
    catalog_probe_ok = False
    specific_resolution = None
    used_brand_candidates = False
    used_category_candidates = False
    catalog_index_primary = False
    catalog_index_strategy: str | None = None
    catalog_index_seeded = 0
    search_term_count = len(specific_product_search_terms(interpretation))
    catalog_discovered_count = 0
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

    def _accumulation_limit() -> int:
        if retrieval_plan.mode == "exact":
            return (
                retrieval_plan.discovery_max_products
                + retrieval_plan.candidate_limit
            )
        return retrieval_plan.candidate_limit

    def _absorb_products(
        raw_products: list[Any],
        *,
        catalog_discovery: bool = False,
        prefer_color: bool = False,
    ) -> None:
        nonlocal catalog_discovered_count
        color_tokens = preference_color_tokens(interpretation)
        for product in raw_products:
            if not isinstance(product, dict) or product.get("id") is None:
                continue
            product_id = str(product["id"])
            if product_id in seen_ids:
                continue
            is_color_hit = bool(
                color_tokens
                and product_matches_color_tokens(product, color_tokens)
            )
            # Color harvest must not be dropped because brand paging filled
            # the pool with Kingfisher/Dagger siblings first.
            at_limit = len(candidates) >= _accumulation_limit()
            if at_limit and not (prefer_color and is_color_hit):
                continue
            if at_limit and prefer_color and is_color_hit:
                # Evict a non-color sibling to make room.
                for index, existing in enumerate(candidates):
                    if not product_matches_color_tokens(existing, color_tokens):
                        evicted_id = str(existing.get("id"))
                        candidates.pop(index)
                        seen_ids.discard(evicted_id)
                        break
                else:
                    continue
            seen_ids.add(product_id)
            candidates.append(product)
            if catalog_discovery:
                catalog_discovered_count += 1

    def _refresh_hard_filtered() -> None:
        nonlocal hard_filtered
        if retrieval_plan.mode == "exact":
            hard_filtered = exact_progress_matches(candidates, interpretation)
        else:
            hard_filtered = hard_filter_products(
                candidates,
                interpretation,
                mode=retrieval_plan.mode,
            )

    async def _run_probe(request: Any) -> tuple[Any, dict[str, Any]]:
        arguments = {
            **request.tool_arguments(),
            "limit": request.limit,
            "page": request.page,
        }
        print("[sales.retrieval.request]", {
            "strategy": request.strategy,
            "category_id_present": bool(request.category_id),
            "name_filter_present": bool(request.name),
            "has_brand_filter": bool(request.brand),
            "token_count": len(getattr(request, "tokens", ()) or ()),
            "has_budget_filter": has_budget,
            "candidate_limit": request.limit,
        })
        result = await _call_execute_tool("search_products", arguments)
        return request, result

    # IQ-06: durable catalog index first for recommendations; Tray only refreshes
    # when the index pool is empty or too thin after hard constraints.
    if retrieval_plan.mode == "recommendation":
        from ..catalog_index_primary import (
            fetch_primary_index_candidates,
            index_pool_is_sufficient,
        )

        index_products, catalog_index_strategy = fetch_primary_index_candidates(
            interpretation,
            limit=max(
                retrieval_plan.candidate_limit,
                int(
                    getattr(
                        get_settings(),
                        "agent_catalog_index_candidate_limit",
                        30,
                    )
                    or 30
                ),
            ),
        )
        if index_products:
            _absorb_products(index_products)
            catalog_index_seeded = len(index_products)
            _refresh_hard_filtered()
            catalog_index_primary = index_pool_is_sufficient(
                hard_filtered,
                candidate_limit=retrieval_plan.candidate_limit,
            )
            print(
                "[catalog.index.primary]",
                {
                    "strategy": catalog_index_strategy,
                    "seeded": catalog_index_seeded,
                    "hard_filtered": len(hard_filtered),
                    "sufficient": catalog_index_primary,
                    "skip_tray_fanout": catalog_index_primary,
                },
            )
        elif bool(getattr(get_settings(), "agent_catalog_index_read_enabled", True)):
            print(
                "[catalog.index.primary]",
                {
                    "strategy": None,
                    "seeded": 0,
                    "hard_filtered": 0,
                    "sufficient": False,
                    "skip_tray_fanout": False,
                    "reason": "catalog_index_empty_or_unavailable",
                },
            )

    if catalog_index_primary:
        print(
            "[catalog.index.primary.skip_tray]",
            {
                "probe_count": 0,
                "discovery_count": 0,
                "candidate_count": len(candidates),
                "hard_filtered_count": len(hard_filtered),
            },
        )
    elif probe_requests:
        if catalog_index_seeded:
            print(
                "[catalog.index.refresh]",
                {
                    "reason": "index_insufficient_or_stale",
                    "prior_seeded": catalog_index_seeded,
                    "prior_hard_filtered": len(hard_filtered),
                    "strategy": catalog_index_strategy,
                },
            )
        probe_results = await asyncio.gather(
            *[_run_probe(request) for request in probe_requests]
        )
        for request, result in probe_results:
            if "error" in result:
                product_lookup_failed = True
                continue
            catalog_probe_ok = True
            raw_products = (
                result.get("products")
                if isinstance(result.get("products"), list)
                else []
            )
            _absorb_products(raw_products)
            print("[sales.retrieval.result]", {
                "strategy": request.strategy,
                "raw_candidate_count": len(raw_products),
                "hard_filtered_count": None,
            })
        _refresh_hard_filtered()
        print("[sales.product.resolve]", {
            "strategy": "parallel_probes",
            "has_brand": bool(interpretation.subject.brand),
            "has_model": bool(interpretation.subject.model),
            "candidate_count": len(candidates),
            "matched_count": len(hard_filtered),
            "probe_count": len(probe_requests),
        })

    if not hard_filtered or retrieval_plan.mode != "exact":
        for request in discovery_requests:
            if retrieval_plan.mode == "exact" and hard_filtered:
                break
            if (
                retrieval_plan.mode == "recommendation"
                and len(candidates) >= retrieval_plan.candidate_limit
            ):
                break
            catalog_discovery = True
            pages = range(1, retrieval_plan.discovery_max_pages + 1)
            for page in pages:
                page_limit = retrieval_plan.discovery_page_limit
                arguments = {
                    **request.tool_arguments(),
                    "limit": page_limit,
                    "page": page,
                }
                print("[sales.retrieval.request]", {
                    "strategy": request.strategy,
                    "category_id_present": bool(request.category_id),
                    "name_filter_present": bool(request.name),
                    "has_brand_filter": bool(request.brand),
                    "has_budget_filter": has_budget,
                    "candidate_limit": page_limit,
                })
                result = await _call_execute_tool("search_products", arguments)
                used_brand_candidates = (
                    used_brand_candidates
                    or request.strategy == "brand_candidates"
                )
                used_category_candidates = (
                    used_category_candidates
                    or request.strategy == "category_candidates"
                )
                if "error" in result:
                    product_lookup_failed = True
                    break
                catalog_probe_ok = True
                raw_products = (
                    result.get("products")
                    if isinstance(result.get("products"), list)
                    else []
                )
                _absorb_products(raw_products, catalog_discovery=True)
                _refresh_hard_filtered()
                print("[sales.retrieval.result]", {
                    "strategy": request.strategy,
                    "raw_candidate_count": len(raw_products),
                    "hard_filtered_count": len(hard_filtered),
                })
                print("[sales.product.resolve]", {
                    "strategy": (
                        "brand_candidates"
                        if request.strategy == "brand_candidates"
                        else "category_candidates"
                    ),
                    "has_brand": bool(interpretation.subject.brand),
                    "has_model": bool(interpretation.subject.model),
                    "candidate_count": len(candidates),
                    "matched_count": len(hard_filtered),
                })
                paging = (
                    result.get("paging")
                    if isinstance(result.get("paging"), dict)
                    else {}
                )
                try:
                    total = (
                        int(paging["total"])
                        if paging.get("total") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    total = None
                try:
                    response_limit = int(paging.get("limit") or page_limit)
                except (TypeError, ValueError):
                    response_limit = page_limit
                consumed = page * max(response_limit, 1)
                has_more = bool(raw_products) and (
                    consumed < total
                    if total is not None
                    else len(raw_products) >= page_limit
                )
                print("[sales.catalog.discovery]", {
                    "strategy": (
                        "brand" if request.strategy == "brand_candidates"
                        else "category"
                    ),
                    "brand_present": bool(request.brand),
                    "category_present": bool(request.category_id),
                    "search_term_count": search_term_count,
                    "page": page,
                    "limit": page_limit,
                    "returned_count": len(raw_products),
                    "accumulated_count": catalog_discovered_count,
                    "total_if_known": total,
                })
                if (
                    hard_filtered
                    or not has_more
                    or catalog_discovered_count
                    >= retrieval_plan.discovery_max_products
                ):
                    break
            if retrieval_plan.mode == "exact" and hard_filtered:
                break

    # Tier 2.5 — if color still missing, reuse family codes seen on siblings
    # (e.g. C63 from other Sealander titles) to probe the exact color title.
    if (
        retrieval_plan.mode == "exact"
        and not hard_filtered
        and candidates
        and preference_color_tokens(interpretation)
    ):
        color_hue = " ".join(preference_color_tokens(interpretation)).title()
        color_labels = [
            label.title() if label.isalpha() else label
            for label in preference_color_search_labels(interpretation)
        ] or ([color_hue] if color_hue else [])
        core = " ".join(
            identity_core_tokens(
                interpretation.subject.model,
                color_tokens=preference_color_tokens(interpretation),
            )[:4]
        ).title()
        auto_bit = (
            "Automático"
            if re.search(
                r"\b(automatic|automatico)\b",
                (interpretation.subject.model or "").casefold(),
            )
            else None
        )
        family_codes = infer_family_codes_from_candidates(
            candidates,
            interpretation,
        )
        enrich_names: list[str] = []
        # Shortest probes first — Tray name search often misses long titles.
        for code in family_codes:
            enrich_names.append(f"{code} {color_hue}".strip())
            if core:
                enrich_names.append(f"{code} {core} {color_hue}".strip())
            enrich_names.append(
                " ".join(
                    part for part in (code, core, auto_bit, color_hue) if part
                )
            )
            enrich_names.append(
                " ".join(
                    part
                    for part in (
                        "Relógio",
                        interpretation.subject.brand,
                        code,
                        core,
                        auto_bit,
                        color_hue,
                    )
                    if part
                )
            )
        if core:
            enrich_names.append(f"{core} {color_hue}".strip())
        enrich_names = list(dict.fromkeys(n for n in enrich_names if n))[:6]
        brand = (interpretation.subject.brand or "").strip()
        enrich_calls: list[dict[str, Any]] = [
            {"name": name, "limit": 20, "page": 1}
            for name in enrich_names
        ]
        if enrich_calls:
            print("[sales.retrieval.family_enrich]", {
                "family_codes": list(family_codes),
                "probe_count": len(enrich_calls),
            })
            enrich_results = await asyncio.gather(
                *[
                    _call_execute_tool("search_products", call)
                    for call in enrich_calls
                ]
            )
            for result in enrich_results:
                if "error" in result:
                    product_lookup_failed = True
                    continue
                catalog_probe_ok = True
                raw_products = (
                    result.get("products")
                    if isinstance(result.get("products"), list)
                    else []
                )
                _absorb_products(raw_products, prefer_color=True)
            _refresh_hard_filtered()

        # Tier 2.6 — page brand+color aliases (azul/blue) until hue lands in pool.
        if brand and color_labels and not hard_filtered:
            color_pages_hits = 0
            for label in color_labels[:4]:
                for page in range(1, 4):
                    print("[sales.retrieval.color_harvest]", {
                        "color": label,
                        "page": page,
                    })
                    result = await _call_execute_tool(
                        "search_products",
                        {
                            "name": label,
                            "brand": brand,
                            "limit": 20,
                            "page": page,
                        },
                    )
                    if "error" in result:
                        product_lookup_failed = True
                        break
                    catalog_probe_ok = True
                    raw_products = (
                        result.get("products")
                        if isinstance(result.get("products"), list)
                        else []
                    )
                    before = len(candidates)
                    _absorb_products(raw_products, prefer_color=True)
                    color_pages_hits += max(0, len(candidates) - before)
                    _refresh_hard_filtered()
                    if hard_filtered or not raw_products:
                        break
                if hard_filtered:
                    break
            print("[sales.retrieval.color_harvest.done]", {
                "colors": color_labels[:4],
                "absorbed_colorish": color_pages_hits,
                "hard_filtered": len(hard_filtered),
            })

    # Merge durable brand catalog cache before preference ranking.
    if (
        not catalog_index_primary
        and retrieval_plan.mode == "recommendation"
        and interpretation.subject.brand
    ):
        candidates = await ensure_brand_pool_in_candidates(
            brand=interpretation.subject.brand,
            candidates=candidates,
            seen_ids=seen_ids,
            execute_tool=_call_execute_tool,
            limit=max(retrieval_plan.candidate_limit, 120),
        )
        _refresh_hard_filtered()
        print("[sales.retrieval.catalog_cache]", {
            "brand": interpretation.subject.brand,
            "candidate_count": len(candidates),
            "hard_filtered_count": len(hard_filtered),
        })

    # Recommendation mode with only probe requests (no discovery strategies).
    if retrieval_plan.mode == "recommendation" and not discovery_requests:
        _refresh_hard_filtered()

    if retrieval_plan.mode == "exact" and candidates:
        # Score the full discovered pool (Tier 1 probes, then Tier 2 pages).
        require_color = bool(preference_color_tokens(interpretation))
        scored_hits = score_catalog_candidates(
            candidates,
            interpretation,
            require_color=require_color,
            allow_movement_mismatch=False,
            limit=retrieval_plan.candidate_limit,
        )
        matcher_candidates = scored_hits or prefilter_specific_candidates(
            candidates,
            interpretation,
            limit=retrieval_plan.candidate_limit,
        )
        print("[sales.catalog.prefilter]", {
            "discovered_count": len(candidates),
            "shortlisted_count": len(matcher_candidates),
            "keyword_hit_count": len(scored_hits),
        })
        try:
            specific_resolution = await match_specific_products(
                matcher_candidates if scored_hits else candidates,
                interpretation,
            )
            hard_filtered = list(specific_resolution.products)
        except ProductMatchError:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_match_failed",
            )
        print("[sales.product.disambiguation]", {
            "candidate_pool_count": len(candidates),
            "plausible_count": len(hard_filtered),
            "match_status": specific_resolution.status,
            "used_brand_candidates": used_brand_candidates,
            "used_category_candidates": used_category_candidates,
        })

    if not candidates:
        if category_resolution and category_resolution.lookup_failed:
            category_failure = (
                category_resolution.failure_reason or "category_adapter_error"
            )
            print("[sales.retrieval.empty]", {"reason": category_failure})
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason=category_failure,
            )
        if product_lookup_failed and not catalog_probe_ok:
            print("[sales.retrieval.empty]", {"reason": "catalog_lookup_failed"})
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
        reason = "exact_product_not_found" if retrieval_plan.mode == "exact" else "catalog_empty"
        print("[sales.retrieval.empty]", {"reason": reason, "had_catalog_ok": catalog_probe_ok})
        if retrieval_plan.mode == "exact":
            return AgentResult(
                reply_text="Não encontrei esse produto no catálogo agora.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_not_found",
            )
        return AgentResult(
            reply_text="Não encontrei opções disponíveis para esses critérios agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="recommendation_no_match",
        )
    if not hard_filtered:
        reason = "exact_product_not_found" if retrieval_plan.mode == "exact" else "hard_filter_empty"
        print("[sales.retrieval.empty]", {"reason": reason})
        if retrieval_plan.mode == "exact":
            if product_lookup_failed and not catalog_probe_ok:
                return AgentResult(
                    reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="tray_adapter_unavailable",
                )
            brand = (interpretation.subject.brand or "").strip()
            if used_brand_candidates and brand and candidates:
                soft = soft_confirm_candidates(
                    candidates,
                    interpretation,
                    limit=customer_result_limit(),
                )
                if soft:
                    refreshed, revalidation_failed = await revalidate_products(
                        soft,
                        interpretation,
                        _call_execute_tool,
                    )
                    if refreshed or not revalidation_failed:
                        from ..commerce_router import _product_lines

                        final_products = refreshed or soft
                        numbered_lines = [
                            f"{position}. {line}"
                            for position, line in enumerate(
                                _product_lines(final_products, compact=True),
                                start=1,
                            )
                        ]
                        return AgentResult(
                            reply_text=(
                                f"Não achei a combinação exata da foto, mas estes "
                                f"{brand} da mesma linha são os mais próximos:\n"
                                + "\n".join(numbered_lines[:2])
                                + "\n\nQuer ver algum desses, ou prefere outra cor/modelo?"
                            ),
                            intent="commerce",
                            handoff_required=False,
                            safety_reason="exact_product_ambiguous_brand",
                            commercial_data={
                                "products": final_products,
                                "match_status": "ambiguous",
                            },
                            response_metadata={
                                "presented_products": True,
                                "product_resolution_state": "plausible_matches",
                                "clear_active_product": True,
                            },
                        )
                return AgentResult(
                    reply_text=(
                        f"Não confirmei essa referência exata agora, mas tenho peças {brand} no catálogo. "
                        "Quer que eu mostre algumas opções próximas?"
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="exact_product_ambiguous_brand",
                )
            return AgentResult(
                reply_text="Não encontrei esse produto no catálogo agora.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_not_found",
            )
        return AgentResult(
            reply_text="Encontrei produtos no catálogo, mas nenhum atende aos critérios objetivos informados agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="recommendation_no_match",
        )

    if retrieval_plan.mode == "recommendation":
        from ..catalog_index import hybrid_rank_products, index_products_best_effort

        # Late index merge only when we did not already seed from the durable index.
        if (
            not catalog_index_seeded
            and bool(getattr(get_settings(), "agent_catalog_index_read_enabled", True))
        ):
            from ..catalog_index_primary import fetch_primary_index_candidates

            index_rows, late_strategy = fetch_primary_index_candidates(
                interpretation,
                limit=int(
                    getattr(
                        get_settings(),
                        "agent_catalog_index_candidate_limit",
                        30,
                    )
                    or 30
                ),
            )
            if index_rows:
                before = len(candidates)
                _absorb_products(index_rows)
                _refresh_hard_filtered()
                seeded = len(candidates) - before
                if seeded:
                    catalog_index_seeded = seeded
                    catalog_index_strategy = late_strategy or catalog_index_strategy
                    print(
                        "[catalog.index.read]",
                        {
                            "strategy": catalog_index_strategy,
                            "seeded": seeded,
                            "fallback": "merged_with_tray_pool",
                        },
                    )
            elif bool(
                getattr(get_settings(), "agent_catalog_index_fallback_to_tray", True)
            ):
                print(
                    "[catalog.index.fallback]",
                    {"reason": "catalog_index_empty_or_unavailable"},
                )

        factual_source = (
            "catalog_index" if catalog_index_primary else "tray_search"
        )
        # Hybrid hard/soft ranking over the filtered pool (no free LLM catalog search).
        hard_filtered = hybrid_rank_products(
            hard_filtered,
            interpretation,
            mode="recommendation",
            factual_source=factual_source,
        )
        if bool(getattr(get_settings(), "agent_catalog_index_write_enabled", True)):
            # Refresh durable index from Tray results (or reaffirm index hits).
            index_products_best_effort(
                hard_filtered,
                factual_source=factual_source,
            )
        enriched = await enrich_product_variants(
            hard_filtered,
            interpretation,
            _call_execute_tool,
        )
        ranked = await rerank_products(enriched, interpretation)
    else:
        ranked = hard_filtered
    ranked = apply_persona_presentation_order(ranked)
    selected = ranked[: customer_result_limit()]
    refreshed, revalidation_failed = await revalidate_products(
        selected,
        interpretation,
        _call_execute_tool,
    )
    if not refreshed and revalidation_failed:
        # IQ-06 resilience: if the durable index already produced a usable pool,
        # serve those rows instead of failing the turn when Tray is 503/429.
        index_backed = catalog_index_primary or any(
            bool(product.get("_from_catalog_index"))
            or str(product.get("_factual_source") or "").strip().lower()
            == "catalog_index"
            for product in selected
        )
        if index_backed and selected:
            print(
                "[sales.revalidate.index_fallback]",
                {
                    "selected": len(selected),
                    "catalog_index_primary": catalog_index_primary,
                    "reason": "tray_revalidation_unavailable",
                },
            )
            for product in selected:
                product.setdefault("_factual_source", "catalog_index")
                product["_revalidated"] = False
                product["_revalidation_degraded"] = True
        else:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
    from ..commerce_router import _product_result

    final_products = refreshed or selected
    if retrieval_plan.mode == "exact":
        final_products = [
            {
                **product,
                "availability_state": product_availability_state(product),
            }
            for product in final_products
        ]
        availability_states = [
            str(product["availability_state"])
            for product in final_products
        ]
        if any(state == "available" for state in availability_states):
            availability_state = "available"
        elif availability_states and all(state == "unavailable" for state in availability_states):
            availability_state = "unavailable"
        else:
            availability_state = "unknown"
        print("[sales.product.availability]", {
            "resolved": bool(final_products),
            "available_state": availability_state,
        })
        if specific_resolution and specific_resolution.status == "ambiguous":
            result = _product_result("product_disambiguation", final_products)
            result.commercial_data = {
                "products": final_products,
                "match_status": "ambiguous",
            }
            result.response_metadata.update({
                "presented_products": True,
                "product_resolution_state": "plausible_matches",
                "clear_active_product": True,
            })
            return result
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
                    "products": final_products,
                    "availability_state": availability_state,
                },
                response_metadata={
                    "presented_products": True,
                    "product_resolution_state": "found_unavailable",
                },
            )
    result = _product_result("product_search", final_products)
    result.response_metadata["presented_products"] = True
    try:
        from ..catalog_index import build_allowed_id_sets

        allowed = build_allowed_id_sets(final_products)
        result.response_metadata["allowed_id_sets"] = {
            key: sorted(values) for key, values in allowed.items()
        }
        result.response_metadata["tenant_id"] = str(
            getattr(get_settings(), "agent_persona_tenant_id", None) or "newstore"
        )
    except Exception:
        pass
    if retrieval_plan.mode == "exact":
        result.response_metadata["product_resolution_state"] = (
            "found_available" if availability_state == "available" else "found_unknown"
        )
        if result.commercial_data is not None:
            result.commercial_data["availability_state"] = availability_state
    elif not result.response_metadata.get("product_resolution_state"):
        result.response_metadata["product_resolution_state"] = "options_presented"
    return result




_execute_contextual_product_lookup = execute_contextual_product_lookup
_execute_compiled_product_retrieval = execute_compiled_product_retrieval
