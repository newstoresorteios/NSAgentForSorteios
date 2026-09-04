from __future__ import annotations

import json
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from app.models import SalesInterpretation
from app.llm.openai_runtime import execute_openai_call
from app.ops.turn_runtime import LLMCallBudgetExceeded
import app.catalog.retrieval.runtime as _runtime
from app.catalog.retrieval.limits import GPT_MATCH_CANDIDATE_LIMIT, RERANK_SELECTION_LIMIT
from app.catalog.retrieval.scoring import (
    _brand_compatible_candidates,
    compact_candidates,
    exact_specific_product_matches,
    prefilter_specific_candidates,
    score_catalog_candidates,
)
from app.catalog.retrieval.text import _fold
from app.catalog.retrieval.tokens import (
    catalog_match_tokens,
    extract_reference_code,
    model_excludes_gmt,
    preference_color_tokens,
    product_compatible_with_requested_movement,
)
from app.catalog.retrieval.types import (
    ProductMatchError,
    ProductMatchSelection,
    SpecificProductResolution,
)

async def resolve_products_from_message_text(
    text: str | None,
    *,
    execute_tool: Any,
) -> list[dict[str, Any]]:
    """Last-chance identity: storefront URL slug, reference code, or URL tokens."""
    blob = str(text or "").strip()
    if not blob or execute_tool is None:
        return []

    from app.catalog.specs.catalog_specs import reference_from_store_url
    from app.stories.story_product_matcher import extract_store_product_url, tokens_from_store_url

    products: list[dict[str, Any]] = []
    seen: set[str] = set()

    async def _absorb(raw: Any) -> None:
        if not isinstance(raw, dict) or raw.get("error") or raw.get("id") is None:
            return
        product_id = str(raw["id"])
        if product_id in seen:
            return
        seen.add(product_id)
        products.append(raw)

    async def _absorb_search(result: Any) -> None:
        if not isinstance(result, dict) or "error" in result:
            return
        for item in result.get("products") or []:
            if isinstance(item, dict):
                await _absorb(item)

    tenant_id = (
        getattr(_runtime.get_settings(), "agent_persona_tenant_id", None) or "newstore"
    )

    store_url = extract_store_product_url(blob)
    if store_url:
        ref = reference_from_store_url(store_url)
        if ref:
            await _absorb_search(
                await execute_tool(
                    "search_products",
                    {"reference": ref, "limit": 10, "page": 1},
                )
            )
            if not products:
                try:
                    from app.catalog.index.repository import CatalogIndexRepository

                    rows = CatalogIndexRepository().search_exact(
                        tenant_id=tenant_id,
                        reference=ref,
                        limit=5,
                    )
                    for row in rows or []:
                        pid = str(row.get("product_id") or "")
                        if not pid or pid in seen:
                            continue
                        await _absorb(
                            await execute_tool("get_product", {"product_id": pid})
                        )
                except Exception as exc:
                    from app.catalog.retrieval.runtime import log_swallowed

                    log_swallowed("specific.index_exact", exc)
        if not products:
            brand, tokens = tokens_from_store_url(store_url)
            probe_name = " ".join(tokens[:8]).strip()
            if probe_name:
                args: dict[str, Any] = {
                    "name": probe_name,
                    "limit": 15,
                    "page": 1,
                }
                if brand:
                    args["brand"] = brand
                await _absorb_search(
                    await execute_tool("search_products", args)
                )

    ref = extract_reference_code(blob)
    if ref and not products:
        await _absorb_search(
            await execute_tool(
                "search_products",
                {"reference": ref, "limit": 10, "page": 1},
            )
        )

    if products:
        print(
            "[sales.retrieval.message_identity]",
            {
                "had_url": bool(store_url),
                "had_reference": bool(ref),
                "resolved": len(products),
                "ids": [str(p.get("id")) for p in products[:5]],
            },
        )
    return products


async def match_specific_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> SpecificProductResolution:
    compatible = _brand_compatible_candidates(products, interpretation)
    color_tokens = preference_color_tokens(interpretation)
    # Local keyword score first — works for any brand/title shape.
    scored = score_catalog_candidates(
        compatible,
        interpretation,
        require_color=bool(color_tokens),
        allow_movement_mismatch=False,
        limit=RERANK_SELECTION_LIMIT,
    )
    if scored:
        selected = scored[:RERANK_SELECTION_LIMIT]
        status: Literal["exact", "ambiguous", "none"] = (
            "exact" if len(selected) == 1 else "ambiguous"
        )
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "keyword_score",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="exact",
        )

    exact_matches = exact_specific_product_matches(compatible, interpretation)
    if exact_matches and not color_tokens:
        selected = exact_matches[:RERANK_SELECTION_LIMIT]
        status = "exact" if len(selected) == 1 else "ambiguous"
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="exact",
        )
    if (
        model_excludes_gmt(interpretation.subject.model)
        and compatible
        and not any(
            product_compatible_with_requested_movement(
                product,
                interpretation.subject.model,
                interpretation.preferences.attributes,
            )
            for product in compatible
        )
    ):
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "movement_mismatch",
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )

    settings = _runtime.get_settings()
    if not compatible:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": 0,
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )
    if not settings.openai_api_key:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": "OpenAIUnavailable",
            "reason": "color_mismatch" if color_tokens else "openai_unavailable",
        })
        # Color asked but only other dials in pool — never soft-substitute.
        if color_tokens:
            return SpecificProductResolution(
                status="none",
                products=(),
                match_source="exact",
            )
        raise ProductMatchError("specific_product_match_unavailable")

    # Local keyword miss (common when Vision says "rosa claro" and the catalog
    # title only has "Rosa"): ask GPT to normalize against the full query list.
    candidate_by_id = {
        str(product["id"]): product
        for product in compatible
        if product.get("id") is not None
    }
    gpt_pool = compact_candidates(
        compatible,
        limit=GPT_MATCH_CANDIDATE_LIMIT,
    )
    print("[sales.product.match]", {
        "candidate_count": len(compatible),
        "gpt_pool_count": len(gpt_pool),
        "selected_count": 0,
        "invalid_ids_count": 0,
        "match_source": "openai",
        "reason": "gpt_catalog_normalize",
        "has_color": bool(color_tokens),
    })
    try:
        from app.llm.openai_errors import OpenAIGatewayError
        from app.llm.openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=ProductMatchSelection,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Resolva o produto pedido usando SOMENTE itens de CANDIDATES. "
                        "Normalize nomes em inglês/PT (Automatic→Automático, pink→Rosa) e "
                        "ignore descritores de tom (claro, escuro, mostrador). "
                        "Se PREFERENCES.color existir (ex.: rosa), escolha o título que "
                        "contenha essa cor; NÃO substitua por outra cor da mesma linha "
                        "(Kingfisher/Dagger/Azul no lugar de Rosa). "
                        "Use match_status=exact só com um único ID seguro; ambiguous se "
                        "houver 2+ opções da cor/modelo pedidos; none se a cor/modelo "
                        "não estiver na lista. candidate_ids e best_candidate_id devem "
                        "ser IDs de CANDIDATES."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "SUBJECT": interpretation.subject.model_dump(
                                mode="json",
                                exclude_none=True,
                            ),
                            "PREFERENCES": interpretation.preferences.model_dump(
                                mode="json",
                                exclude_none=True,
                            ),
                            "SEARCH_TOKENS": list(
                                catalog_match_tokens(interpretation)
                            ),
                            "CANDIDATES": gpt_pool,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            call_type="product_selection",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, ProductMatchSelection):
            raise ValueError("product_match_schema_missing")
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        invalid_ids = 0
        for product_id in parsed.candidate_ids[:RERANK_SELECTION_LIMIT]:
            normalized_id = str(product_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            product = candidate_by_id.get(normalized_id)
            if product is None:
                invalid_ids += 1
                continue
            selected.append(product)
        best_candidate_id = (
            str(parsed.best_candidate_id)
            if parsed.best_candidate_id is not None
            else None
        )
        if best_candidate_id is not None and best_candidate_id not in candidate_by_id:
            invalid_ids += 1
            best_candidate_id = None

        status: Literal["exact", "ambiguous", "none"] = "none"
        if parsed.match_status == "exact":
            exact_id = best_candidate_id
            if exact_id is None and len(selected) == 1:
                exact_id = str(selected[0].get("id"))
            exact_product = candidate_by_id.get(exact_id or "")
            if exact_product is not None:
                selected = [exact_product]
                status = "exact"
            else:
                selected = []
        elif parsed.match_status == "ambiguous" and len(selected) >= 2:
            status = "ambiguous"
        else:
            selected = []
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "match_source": "openai",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": invalid_ids,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="openai",
            invalid_ids_count=invalid_ids,
        )
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": type(exc).__name__,
        })
        raise ProductMatchError("specific_product_match_failed") from exc
