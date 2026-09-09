"""Index seed, Tray probes, and brand/category discovery paging."""

from __future__ import annotations

import asyncio
from typing import Any

import app.catalog.retrieval.runtime as _runtime
from app.catalog.retrieval.session import RetrievalSession


async def seed_from_catalog_index(session: RetrievalSession) -> None:
    plan = session.retrieval_plan
    interpretation = session.interpretation
    if plan.mode not in {"recommendation", "exact"}:
        return
    from app.catalog.index.primary import (  # late: tests patch this module
        fetch_primary_index_candidates,
        index_pool_is_sufficient,
    )

    index_products, session.catalog_index_strategy = fetch_primary_index_candidates(
        interpretation,
        limit=max(
            plan.candidate_limit,
            int(
                getattr(
                    _runtime.get_settings(),
                    "agent_catalog_index_candidate_limit",
                    30,
                )
                or 30
            ),
        ),
        message_text=session.message_text,
    )
    if index_products:
        session.absorb_products(index_products)
        session.catalog_index_seeded = len(index_products)
        session.refresh_hard_filtered()
        if plan.mode == "exact":
            session.catalog_index_primary = bool(
                session.hard_filtered
            ) and index_pool_is_sufficient(
                session.hard_filtered,
                candidate_limit=min(plan.candidate_limit, 5),
            )
        else:
            session.catalog_index_primary = index_pool_is_sufficient(
                session.hard_filtered,
                candidate_limit=plan.candidate_limit,
            )
        print(
            "[catalog.index.primary]",
            {
                "strategy": session.catalog_index_strategy,
                "mode": plan.mode,
                "seeded": session.catalog_index_seeded,
                "hard_filtered": len(session.hard_filtered),
                "sufficient": session.catalog_index_primary,
                "skip_tray_fanout": session.catalog_index_primary,
            },
        )
        # A sufficient hard-filtered index pool is the retrieval. Tray list
        # fan-out on every budget/color/size turn emptied the turn (adaptor
        # 500) and skipped GPT. Live prices still come from GET /products/{id}
        # during revalidate of the top-N shown.
    elif bool(getattr(_runtime.get_settings(), "agent_catalog_index_read_enabled", True)):
        print(
            "[catalog.index.primary]",
            {
                "strategy": None,
                "mode": plan.mode,
                "seeded": 0,
                "hard_filtered": 0,
                "sufficient": False,
                "skip_tray_fanout": False,
                "reason": "catalog_index_empty_or_unavailable",
            },
        )


async def run_probes(
    session: RetrievalSession,
    probe_requests: list[Any],
) -> None:
    if session.catalog_index_primary:
        print(
            "[catalog.index.primary.skip_tray]",
            {
                "probe_count": 0,
                "discovery_count": 0,
                "candidate_count": len(session.candidates),
                "hard_filtered_count": len(session.hard_filtered),
            },
        )
        return
    if not probe_requests:
        return
    if session.catalog_index_seeded:
        print(
            "[catalog.index.refresh]",
            {
                "reason": "index_insufficient_or_stale",
                "prior_seeded": session.catalog_index_seeded,
                "prior_hard_filtered": len(session.hard_filtered),
                "strategy": session.catalog_index_strategy,
            },
        )

    priority = {
        "exact_ean": 0,
        "exact_reference": 0,
        "exact_model_code": 0,
        "exact_model_with_brand": 1,
        "exact_model": 1,
        "exact_color_family_code": 1,
        "exact_color_core": 2,
        "exact_color_automatic": 2,
        "exact_catalog_title": 2,
        "exact_query_reference_context": 3,
        "exact_query_full": 3,
        "token_and_search": 4,
        "token_and_search_no_color": 5,
        "token_and_search_short": 5,
    }
    ordered_requests = sorted(
        enumerate(probe_requests),
        key=lambda item: (priority.get(item[1].strategy, 10), item[0]),
    )

    async def _run_probe(request: Any) -> tuple[Any, dict[str, Any]]:
        arguments = {
            **session.list_query_extras(session.interpretation),
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
            "has_budget_filter": session.has_budget,
            "candidate_limit": request.limit,
        })
        result = await session.search_products(arguments)
        return request, result

    attempted = 0
    # Exact lookups are cheapest when the highest-confidence request runs
    # alone: a reference/EAN hit should not dispatch a speculative sibling
    # request. Recommendation probes use pairs to retain reasonable latency.
    batch_size = 1 if session.retrieval_plan.mode == "exact" else 2
    probe_call_limit = (
        min(2, int(session.search_call_limit or 1))
        if session.retrieval_plan.mode == "exact"
        else int(session.search_call_limit or 1)
    )
    for offset in range(0, len(ordered_requests), batch_size):
        remaining = min(
            int(session.search_call_limit or 1) - session.search_call_count,
            probe_call_limit - attempted,
        )
        if remaining <= 0:
            if session.search_call_count >= int(session.search_call_limit or 1):
                session.search_budget_exhausted = True
            break
        batch = [request for _, request in ordered_requests[offset : offset + min(batch_size, remaining)]]
        probe_results = await asyncio.gather(*[_run_probe(request) for request in batch])
        attempted += len(batch)
        for request, result in probe_results:
            if "error" in result:
                if not result.get("budget_exhausted"):
                    session.product_lookup_failed = True
                continue
            session.catalog_probe_ok = True
            raw_products = (
                result.get("products")
                if isinstance(result.get("products"), list)
                else []
            )
            session.absorb_products(raw_products)
            print("[sales.retrieval.result]", {
                "strategy": request.strategy,
                "raw_candidate_count": len(raw_products),
                "hard_filtered_count": None,
            })
        session.refresh_hard_filtered()
        if session.hard_filtered:
            if session.retrieval_plan.mode == "exact":
                break
            target = min(
                session.retrieval_plan.candidate_limit,
                3,
            )
            from app.catalog.specs.catalog_specs import message_requests_other_brands

            distinct_brands = {
                str(product.get("brand") or "").strip().casefold()
                for product in session.hard_filtered
                if str(product.get("brand") or "").strip()
            }
            diversity_satisfied = (
                len(distinct_brands) >= target
                if message_requests_other_brands(session.message_text)
                else True
            )
            if len(session.hard_filtered) >= target and diversity_satisfied:
                break
    print("[sales.product.resolve]", {
        "strategy": "parallel_probes",
        "has_brand": bool(session.interpretation.subject.brand),
        "has_model": bool(session.interpretation.subject.model),
        "candidate_count": len(session.candidates),
        "matched_count": len(session.hard_filtered),
        "probe_count": attempted,
        "probe_planned_count": len(probe_requests),
        "early_stop": attempted < len(probe_requests),
        "search_call_count": session.search_call_count,
        "search_call_limit": session.search_call_limit,
    })


async def run_discovery(
    session: RetrievalSession,
    discovery_requests: list[Any],
    *,
    search_term_count: int,
) -> None:
    plan = session.retrieval_plan
    interpretation = session.interpretation
    if session.hard_filtered and plan.mode == "exact":
        return
    for request in discovery_requests:
        if plan.mode == "exact" and session.hard_filtered:
            break
        if (
            plan.mode == "recommendation"
            and len(session.candidates) >= plan.candidate_limit
        ):
            break
        pages = range(1, plan.discovery_max_pages + 1)
        for page in pages:
            page_limit = plan.discovery_page_limit
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
                "has_budget_filter": session.has_budget,
                "candidate_limit": page_limit,
            })
            result = await session.search_products(arguments)
            session.used_brand_candidates = (
                session.used_brand_candidates
                or request.strategy == "brand_candidates"
            )
            session.used_category_candidates = (
                session.used_category_candidates
                or request.strategy == "category_candidates"
            )
            if "error" in result:
                session.product_lookup_failed = True
                break
            session.catalog_probe_ok = True
            raw_products = (
                result.get("products")
                if isinstance(result.get("products"), list)
                else []
            )
            session.absorb_products(raw_products, catalog_discovery=True)
            session.refresh_hard_filtered()
            print("[sales.retrieval.result]", {
                "strategy": request.strategy,
                "raw_candidate_count": len(raw_products),
                "hard_filtered_count": len(session.hard_filtered),
            })
            print("[sales.product.resolve]", {
                "strategy": (
                    "brand_candidates"
                    if request.strategy == "brand_candidates"
                    else "category_candidates"
                ),
                "has_brand": bool(interpretation.subject.brand),
                "has_model": bool(interpretation.subject.model),
                "candidate_count": len(session.candidates),
                "matched_count": len(session.hard_filtered),
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
                "accumulated_count": session.catalog_discovered_count,
                "total_if_known": total,
            })
            if (
                session.hard_filtered
                or not has_more
                or session.catalog_discovered_count >= plan.discovery_max_products
            ):
                break
        if plan.mode == "exact" and session.hard_filtered:
            break
