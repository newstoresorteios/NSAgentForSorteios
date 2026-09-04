"""Empty-pool and hard-filter-miss recovery (index, message identity, near-match)."""

from __future__ import annotations

from app.catalog.retrieval.limits import customer_result_limit
from app.catalog.retrieval.revalidate import revalidate_products
from app.catalog.retrieval.scoring import (
    prefer_dial_and_case_matches,
    soft_confirm_candidates,
)
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.tokens import (
    preference_color_tokens,
    product_matches_color_tokens,
)
from app.catalog.specs.catalog_specs import (
    interpretation_case_size_range,
    product_case_size_mm,
)
from app.commerce.commerce_router import _product_result, guided_near_match_result
from app.models import AgentResult, SalesInterpretation
import app.catalog.retrieval.runtime as _runtime


async def serve_index_when_tray_unavailable(
    interpretation: SalesInterpretation,
    *,
    reason: str,
    message_text: str | None = None,
) -> AgentResult | None:
    """IQ-06: exact/SKU turns must not die on Tray auth 503 if the durable index has hits."""
    if not bool(getattr(_runtime.get_settings(), "agent_catalog_index_read_enabled", True)):
        return None
    from app.catalog.index.primary import fetch_primary_index_candidates

    index_rows, strategy = fetch_primary_index_candidates(
        interpretation,
        limit=max(customer_result_limit() * 4, 20),
        message_text=message_text,
    )
    if not index_rows:
        print(
            "[sales.retrieval.index_unavailable_miss]",
            {"reason": reason, "strategy": strategy},
        )
        return None

    soft = prefer_dial_and_case_matches(
        soft_confirm_candidates(
            index_rows,
            interpretation,
            limit=customer_result_limit(),
        ),
        interpretation,
        limit=customer_result_limit(),
    )
    if not soft:
        soft = index_rows[: customer_result_limit()]
    for product in soft:
        product.setdefault("_factual_source", "catalog_index")
        product["_from_catalog_index"] = True
        product["_revalidated"] = False
        product["_revalidation_degraded"] = True
    print(
        "[sales.retrieval.index_unavailable_fallback]",
        {
            "reason": reason,
            "strategy": strategy,
            "served": len(soft),
            "ids": [str(p.get("id")) for p in soft[:5]],
        },
    )
    brand = (interpretation.subject.brand or "").strip()
    model = (interpretation.subject.model or "").strip()
    if brand or model:
        return guided_near_match_result(
            soft,
            brand=brand or None,
            limit=customer_result_limit(),
            safety_reason="exact_index_fallback_tray_down",
        )
    return _product_result("product_search", soft)


async def last_chance_from_message_text(
    session: RetrievalSession,
    *,
    mode: str,
) -> AgentResult | None:
    from app.catalog.retrieval.hard_filter import hard_filter_products
    from app.catalog.retrieval.scoring import exact_progress_matches
    from app.catalog.retrieval.specific import resolve_products_from_message_text

    message_text = session.message_text
    interpretation = session.interpretation
    if not (message_text or "").strip():
        return None
    rows = await resolve_products_from_message_text(
        message_text,
        execute_tool=session.execute_tool,
    )
    if not rows:
        return None
    if mode == "exact":
        filtered = exact_progress_matches(rows, interpretation)
        if not filtered:
            filtered = hard_filter_products(
                rows,
                interpretation,
                mode="exact",
            )
    else:
        filtered = hard_filter_products(
            rows,
            interpretation,
            mode="recommendation",
        )
    if not filtered:
        filtered = rows[: customer_result_limit()]
    refreshed, revalidation_failed = await revalidate_products(
        filtered[: customer_result_limit()],
        interpretation,
        session.execute_tool,
    )
    pool = refreshed or filtered
    if not pool:
        return None
    print(
        "[sales.retrieval.message_identity_hit]",
        {
            "mode": mode,
            "served": len(pool),
            "revalidation_degraded": bool(revalidation_failed and refreshed),
        },
    )
    if mode == "exact":
        brand = (interpretation.subject.brand or "").strip()
        return guided_near_match_result(
            pool,
            brand=brand or None,
            limit=customer_result_limit(),
            safety_reason="exact_message_identity_hit",
        )
    return _product_result("product_search", pool[: customer_result_limit()])


async def handle_empty_candidates(session: RetrievalSession) -> AgentResult | None:
    interpretation = session.interpretation
    plan = session.retrieval_plan
    category_resolution = session.category_resolution
    if session.candidates:
        return None
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
    if session.product_lookup_failed and not session.catalog_probe_ok:
        print("[sales.retrieval.empty]", {"reason": "catalog_lookup_failed"})
        fallback = await serve_index_when_tray_unavailable(
            interpretation,
            reason="catalog_lookup_failed",
            message_text=session.message_text,
        )
        if fallback is not None:
            return fallback
        return AgentResult(
            reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
            intent="commerce",
            handoff_required=False,
            safety_reason="tray_adapter_unavailable",
        )
    reason = "exact_product_not_found" if plan.mode == "exact" else "catalog_empty"
    print("[sales.retrieval.empty]", {"reason": reason, "had_catalog_ok": session.catalog_probe_ok})
    if plan.mode == "exact":
        from app.catalog.index.primary import fetch_primary_index_candidates

        index_rows, _strategy = fetch_primary_index_candidates(
            interpretation,
            limit=max(plan.candidate_limit, 30),
            message_text=session.message_text,
        )
        if index_rows:
            soft = prefer_dial_and_case_matches(
                soft_confirm_candidates(
                    index_rows,
                    interpretation,
                    limit=customer_result_limit(),
                ),
                interpretation,
                limit=customer_result_limit(),
            )
            if soft:
                refreshed, revalidation_failed = await revalidate_products(
                    soft,
                    interpretation,
                    session.execute_tool,
                )
                if refreshed or not revalidation_failed:
                    return guided_near_match_result(
                        refreshed or soft,
                        brand=interpretation.subject.brand,
                        limit=customer_result_limit(),
                    )
        identity_hit = await last_chance_from_message_text(session, mode="exact")
        if identity_hit is not None:
            return identity_hit
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


async def handle_hard_filter_miss(session: RetrievalSession) -> AgentResult | None:
    if session.hard_filtered:
        return None
    interpretation = session.interpretation
    plan = session.retrieval_plan
    reason = "exact_product_not_found" if plan.mode == "exact" else "hard_filter_empty"
    print("[sales.retrieval.empty]", {"reason": reason})
    if plan.mode == "recommendation":
        budget_miss = session.budget_hard_miss(interpretation, session.candidates)
        if budget_miss is not None:
            return budget_miss
        if session.product_lookup_failed and not session.catalog_probe_ok:
            fallback = await serve_index_when_tray_unavailable(
                interpretation,
                reason="recommendation_hard_filter_empty_tray_down",
                message_text=session.message_text,
            )
            if fallback is not None:
                return fallback
    if plan.mode == "exact":
        if session.product_lookup_failed and not session.catalog_probe_ok:
            fallback = await serve_index_when_tray_unavailable(
                interpretation,
                reason="exact_hard_filter_empty_tray_down",
                message_text=session.message_text,
            )
            if fallback is not None:
                return fallback
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
        brand = (interpretation.subject.brand or "").strip()
        if brand:
            from app.catalog.index.cache import ensure_brand_pool_in_candidates

            session.candidates = await ensure_brand_pool_in_candidates(
                brand=brand,
                candidates=session.candidates,
                seen_ids=session.seen_ids,
                execute_tool=session.execute_tool,
                limit=max(plan.candidate_limit, 120),
            )
        soft = soft_confirm_candidates(
            session.candidates,
            interpretation,
            limit=customer_result_limit(),
        )
        if soft:
            soft = prefer_dial_and_case_matches(
                soft,
                interpretation,
                limit=customer_result_limit(),
            )
        color_tokens = preference_color_tokens(interpretation)
        if color_tokens and soft:
            color_hits = [
                product
                for product in soft
                if product_matches_color_tokens(product, color_tokens)
            ]
            if color_hits:
                soft = color_hits
        if soft:
            refreshed, revalidation_failed = await revalidate_products(
                soft,
                interpretation,
                session.execute_tool,
            )
            if refreshed or not revalidation_failed:
                return guided_near_match_result(
                    refreshed or soft,
                    brand=brand or None,
                    limit=customer_result_limit(),
                )
        if brand:
            color_hint = ""
            if color_tokens:
                color_hint = f" com {'/'.join(sorted(color_tokens)[:2])}"
            return AgentResult(
                reply_text=(
                    f"Não confirmei essa referência exata de {brand}{color_hint}. "
                    "Você lembra a referência, o tamanho da caixa ou outra cor do visor?"
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
                response_metadata={
                    "product_resolution_state": "needs_clarification",
                    "clear_active_product": True,
                },
            )
        return AgentResult(
            reply_text="Não encontrei esse produto no catálogo agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_not_found",
        )

    case_range = interpretation_case_size_range(
        interpretation,
        message_text=session.message_text,
    )
    if case_range and session.candidates:
        min_mm, max_mm = case_range
        nearby = [
            product
            for product in session.candidates
            if (size := product_case_size_mm(product)) is not None
            and min_mm <= size <= max_mm + 2
        ]
        nearby.sort(key=lambda item: product_case_size_mm(item) or 99)
        if nearby:
            refreshed, revalidation_failed = await revalidate_products(
                nearby,
                interpretation,
                session.execute_tool,
            )
            pool = refreshed or nearby
            if pool and (refreshed or not revalidation_failed):
                result = guided_near_match_result(
                    pool,
                    brand=(interpretation.subject.brand or None),
                    limit=customer_result_limit(),
                    safety_reason="recommendation_near_match",
                )
                if result and min_mm <= (product_case_size_mm(pool[0]) or 0) <= max_mm + 2:
                    if max_mm < (product_case_size_mm(pool[0]) or 0):
                        prefix = (
                            f"Não achei opções exatas entre {min_mm} e {max_mm} mm. "
                            f"As mais próximas que encontrei são:\n\n"
                        )
                        result.reply_text = prefix + (result.reply_text or "")
                return result

    soft = soft_confirm_candidates(
        session.candidates,
        interpretation,
        limit=customer_result_limit(),
    )
    if soft:
        refreshed, revalidation_failed = await revalidate_products(
            soft,
            interpretation,
            session.execute_tool,
        )
        if refreshed or not revalidation_failed:
            return guided_near_match_result(
                refreshed or soft,
                brand=(interpretation.subject.brand or None),
                limit=customer_result_limit(),
                safety_reason="recommendation_near_match",
            )
    return AgentResult(
        reply_text="Encontrei produtos no catálogo, mas nenhum atende aos critérios objetivos informados agora.",
        intent="commerce",
        handoff_required=False,
        safety_reason="recommendation_no_match",
    )
