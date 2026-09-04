"""Family-code probes, color harvest, and durable brand-pool merge."""

from __future__ import annotations

import asyncio
import re

from app.catalog.retrieval.scoring import infer_family_codes_from_candidates
from app.catalog.retrieval.session import RetrievalSession
from app.catalog.retrieval.tokens import (
    identity_core_tokens,
    preference_color_search_labels,
    preference_color_tokens,
)


async def harvest_family_and_color(session: RetrievalSession) -> None:
    plan = session.retrieval_plan
    interpretation = session.interpretation
    if (
        plan.mode != "exact"
        or session.hard_filtered
        or not session.candidates
        or not preference_color_tokens(interpretation)
    ):
        return

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
        session.candidates,
        interpretation,
    )
    enrich_names: list[str] = []
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
    enrich_calls: list[dict] = [
        {"name": name, "limit": 20, "page": 1}
        for name in enrich_names
    ]
    if enrich_calls:
        print("[sales.retrieval.family_enrich]", {
            "family_codes": list(family_codes),
            "probe_count": len(enrich_calls),
        })
        enrich_results = await asyncio.gather(
            *[session.search_products(call) for call in enrich_calls]
        )
        for result in enrich_results:
            if "error" in result:
                session.product_lookup_failed = True
                continue
            session.catalog_probe_ok = True
            raw_products = (
                result.get("products")
                if isinstance(result.get("products"), list)
                else []
            )
            session.absorb_products(raw_products, prefer_color=True)
        session.refresh_hard_filtered()

    if brand and color_labels and not session.hard_filtered:
        color_pages_hits = 0
        for label in color_labels[:4]:
            for page in range(1, 4):
                print("[sales.retrieval.color_harvest]", {
                    "color": label,
                    "page": page,
                })
                result = await session.search_products(
                    {
                        "name": label,
                        "brand": brand,
                        "limit": 20,
                        "page": page,
                    },
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
                before = len(session.candidates)
                session.absorb_products(raw_products, prefer_color=True)
                color_pages_hits += max(0, len(session.candidates) - before)
                session.refresh_hard_filtered()
                if session.hard_filtered or not raw_products:
                    break
            if session.hard_filtered:
                break
        print("[sales.retrieval.color_harvest.done]", {
            "colors": color_labels[:4],
            "absorbed_colorish": color_pages_hits,
            "hard_filtered": len(session.hard_filtered),
        })


async def merge_brand_cache(session: RetrievalSession) -> None:
    interpretation = session.interpretation
    if (
        session.catalog_index_primary
        or session.retrieval_plan.mode != "recommendation"
        or not interpretation.subject.brand
    ):
        return
    from app.catalog.index.cache import ensure_brand_pool_in_candidates

    session.candidates = await ensure_brand_pool_in_candidates(
        brand=interpretation.subject.brand,
        candidates=session.candidates,
        seen_ids=session.seen_ids,
        execute_tool=session.execute_tool,
        limit=max(session.retrieval_plan.candidate_limit, 120),
    )
    session.refresh_hard_filtered()
    print("[sales.retrieval.catalog_cache]", {
        "brand": interpretation.subject.brand,
        "candidate_count": len(session.candidates),
        "hard_filtered_count": len(session.hard_filtered),
    })
