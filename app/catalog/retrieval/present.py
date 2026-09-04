"""Recommendation hybrid rank, revalidation, and AgentResult presentation."""

from __future__ import annotations

from app.catalog.retrieval.availability import (
    apply_persona_presentation_order,
    product_availability_state,
)
from app.catalog.retrieval.limits import customer_result_limit
from app.catalog.retrieval.revalidate import revalidate_products
from app.catalog.retrieval.scoring import prefer_dial_and_case_matches
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.variants import enrich_product_variants
from app.catalog.specs.catalog_specs import (
    excluded_brands_from_interpretation,
    product_matches_excluded_brand,
)
from app.commerce.commerce_router import _product_result
from app.models import AgentResult
import app.catalog.retrieval.runtime as _runtime
from app.catalog.retrieval.runtime import log_swallowed

from app.catalog.retrieval.near_match import serve_index_when_tray_unavailable


async def present_compiled_results(session: RetrievalSession) -> AgentResult:
    interpretation = session.interpretation
    plan = session.retrieval_plan
    hard_filtered = session.hard_filtered

    if plan.mode == "recommendation":
        if (
            not session.catalog_index_seeded
            and bool(getattr(_runtime.get_settings(), "agent_catalog_index_read_enabled", True))
        ):
            from app.catalog.index.primary import fetch_primary_index_candidates

            index_rows, late_strategy = fetch_primary_index_candidates(
                interpretation,
                limit=int(
                    getattr(
                        _runtime.get_settings(),
                        "agent_catalog_index_candidate_limit",
                        30,
                    )
                    or 30
                ),
            )
            if index_rows:
                before = len(session.candidates)
                session.absorb_products(index_rows)
                session.refresh_hard_filtered()
                hard_filtered = session.hard_filtered
                seeded = len(session.candidates) - before
                if seeded:
                    session.catalog_index_seeded = seeded
                    session.catalog_index_strategy = (
                        late_strategy or session.catalog_index_strategy
                    )
                    print(
                        "[catalog.index.read]",
                        {
                            "strategy": session.catalog_index_strategy,
                            "seeded": seeded,
                            "fallback": "merged_with_tray_pool",
                        },
                    )
            elif bool(
                getattr(_runtime.get_settings(), "agent_catalog_index_fallback_to_tray", True)
            ):
                print(
                    "[catalog.index.fallback]",
                    {"reason": "catalog_index_empty_or_unavailable"},
                )

        from app.catalog.index.catalog_index import (
            hybrid_rank_products,
            index_products_best_effort,
        )

        factual_source = (
            "catalog_index" if session.catalog_index_primary else "tray_search"
        )
        hard_filtered = hybrid_rank_products(
            hard_filtered,
            interpretation,
            mode="recommendation",
            factual_source=factual_source,
        )
        hard_filtered = prefer_dial_and_case_matches(
            hard_filtered,
            interpretation,
            limit=max(plan.candidate_limit, customer_result_limit()),
        )
        if bool(getattr(_runtime.get_settings(), "agent_catalog_index_write_enabled", True)):
            index_products_best_effort(
                hard_filtered,
                factual_source=factual_source,
            )
        enriched = await enrich_product_variants(
            hard_filtered,
            interpretation,
            session.execute_tool,
        )
        from app.catalog.retrieval.rerank import rerank_products

        ranked = await rerank_products(enriched, interpretation)
    else:
        ranked = hard_filtered
    ranked = apply_persona_presentation_order(ranked)
    selected = ranked[: customer_result_limit()]
    refreshed, revalidation_failed = await revalidate_products(
        selected,
        interpretation,
        session.execute_tool,
    )
    if not refreshed and revalidation_failed:
        index_backed = session.catalog_index_primary or any(
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
                    "catalog_index_primary": session.catalog_index_primary,
                    "reason": "tray_revalidation_unavailable",
                },
            )
            for product in selected:
                product.setdefault("_factual_source", "catalog_index")
                product["_revalidated"] = False
                product["_revalidation_degraded"] = True
        else:
            fallback = await serve_index_when_tray_unavailable(
                interpretation,
                reason="revalidation_failed_no_index_seed",
            )
            if fallback is not None:
                return fallback
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )

    final_products = refreshed or selected
    try:
        excluded = excluded_brands_from_interpretation(interpretation)
        if excluded and final_products:
            kept = [
                product
                for product in final_products
                if not product_matches_excluded_brand(product, excluded)
            ]
            if len(kept) != len(final_products):
                print(
                    "[sales.retrieval.exclude_brand]",
                    {
                        "before": len(final_products),
                        "after": len(kept),
                        "excluded": excluded[:5],
                    },
                )
            final_products = kept
            if not final_products:
                labels = ", ".join(excluded[:3])
                return AgentResult(
                    reply_text=(
                        f"Entendi — sem {labels}. "
                        "Não achei opções de outras marcas com esses critérios agora. "
                        "Quer ajustar orçamento, tamanho ou estilo?"
                    ),
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="recommendation_no_match",
                    response_metadata={
                        "excluded_brands": excluded,
                        "clear_active_product": True,
                    },
                )
    except Exception as exc:
        log_swallowed("present.excluded_brands", exc)

    availability_state = "unknown"
    if plan.mode == "exact":
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
        elif availability_states and all(
            state == "unavailable" for state in availability_states
        ):
            availability_state = "unavailable"
        else:
            availability_state = "unknown"
        print("[sales.product.availability]", {
            "resolved": bool(final_products),
            "available_state": availability_state,
        })
        if session.specific_resolution and session.specific_resolution.status == "ambiguous":
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
        from app.catalog.index.catalog_index import build_allowed_id_sets

        allowed = build_allowed_id_sets(final_products)
        result.response_metadata["allowed_id_sets"] = {
            key: sorted(values) for key, values in allowed.items()
        }
        result.response_metadata["tenant_id"] = str(
            getattr(_runtime.get_settings(), "agent_persona_tenant_id", None) or "newstore"
        )
    except Exception as exc:
        log_swallowed("present.allowed_id_sets", exc)
    if plan.mode == "exact":
        result.response_metadata["product_resolution_state"] = (
            "found_available" if availability_state == "available" else "found_unknown"
        )
        if result.commercial_data is not None:
            result.commercial_data["availability_state"] = availability_state
    elif not result.response_metadata.get("product_resolution_state"):
        result.response_metadata["product_resolution_state"] = "options_presented"
    return result
