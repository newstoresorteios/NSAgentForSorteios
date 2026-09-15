"""Bounded live confirmation for explicit technical requirements."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.retrieval.limits import customer_result_limit
from app.catalog.retrieval.availability import product_availability_state, apply_persona_presentation_order
from app.catalog.specs.requirements import technical_requirements, feature_evidence, feature_rules, criteria_label
from app.configuration.runtime import policy, message
from app.models import AgentResult
from app.ops.observability import log_event


def technical_miss(interpretation, *, unknown: bool, evidence: list | None = None) -> AgentResult:
    reason = "catalog_requirements_unknown" if unknown else "catalog_requirements_no_match"
    return AgentResult(reply_text=message(reason, criteria=criteria_label(interpretation)), intent="commerce",
        safety_reason=reason, commercial_data={"products": []}, response_metadata={
            "presented_products":False, "product_resolution_state":"technical_unknown" if unknown else "technical_mismatch",
            "clear_active_product":True, "clear_presented_products":True,
            "technical_requirements":technical_requirements(interpretation), "technical_evidence":evidence or [],
        })


def confirmed_product_reply(products, interpretation) -> str:
    from app.commerce.commerce_router import _product_lines
    lines = [f"{i}. {line}" for i, line in enumerate(_product_lines(products, compact=True), 1)]
    return message("catalog_requirements_intro", criteria=criteria_label(interpretation)) + "\n\n" + "\n\n".join(lines)


async def retrieve_technical_products(session) -> AgentResult:
    interpretation = session.interpretation
    required = technical_requirements(interpretation)
    # Search the requested feature itself; a generic category's first page is not
    # a complete search for a technical combination. Keep all probes bounded.
    candidates = list(session.candidates)
    queries = list(dict.fromkeys(r["query"] for r in feature_rules() if required.get(r["field"]) == r["value"] and r.get("query")))
    for query in queries[:int(policy("catalogTechnicalSearchLimit"))]:
        data = await session.search_products({**session.list_query_extras(interpretation), "name":query,
                                             "limit":session.retrieval_plan.candidate_limit, "page":1})
        if data.get("error"):
            if data.get("status_code") in (429, 502, 503, 504):
                break
            continue
        candidates = list(data.get("products") or []) + candidates
    unique = {}
    for product in candidates:
        if isinstance(product, dict) and product.get("id") is not None and str(product["id"]) not in session.excluded_ids:
            unique.setdefault(str(product["id"]), product)
    eligible = hard_filter_products(list(unique.values()), interpretation,
                                    mode=session.retrieval_plan.mode, allow_unknown_features=True)
    eligible.sort(key=lambda p: feature_evidence(p, required)["status"] != "matched")
    limit = int(policy("catalogTechnicalDetailLimit"))
    concurrency = int(policy("catalogTechnicalConcurrency"))
    accepted = []
    evidence = [{"product_id":str(p["id"]), "stage":"candidate", **feature_evidence(p, required)} for p in unique.values()]
    unknown = len(eligible) > limit or not unique or any(e["status"] == "unknown" for e in evidence)
    attempted = 0
    stop = False
    for start in range(0, min(len(eligible), limit), concurrency):
        batch = eligible[start:min(start + concurrency, limit)]
        results = await asyncio.gather(*(session.execute_tool("get_product", {"product_id":str(p["id"])}) for p in batch), return_exceptions=True)
        for prior, live in zip(batch, results):
            attempted += 1
            if isinstance(live, Exception) or live.get("error"):
                unknown = True
                if isinstance(live, Exception) or live.get("status_code") in (429,502,503,504):
                    stop = True
                continue
            # Use the live document for technical/commercial facts; missing
            # properties do not inherit older assertions from the index.
            current = {**live, "id":str(prior["id"])}
            if current.get("name") is None:
                current["name"] = prior.get("name")
            verdict = feature_evidence(current, required)
            evidence.append({"product_id":current["id"], "stage":"live_detail", **verdict})
            unknown |= verdict["status"] == "unknown"
            if verdict["status"] != "matched" or product_availability_state(current) != "available":
                continue
            if not hard_filter_products([current], interpretation, mode=session.retrieval_plan.mode):
                continue
            if not any(current.get(k) is not None for k in ("price", "current_price", "promotional_price")):
                unknown = True
                continue
            current.update(_revalidated=True, _freshness_at=datetime.now(timezone.utc).isoformat(),
                           _factual_source="tray_live", _field_sources={k:"tray_live" for k in current if not k.startswith("_")},
                           _technical_evidence=verdict)
            accepted.append(current)
        if stop or len(accepted) >= customer_result_limit():
            break
    log_event("catalog.technical.confirmation", {"requirements":required, "candidate_count":len(unique),
              "detail_calls":attempted, "confirmed":len(accepted), "unknown":unknown, "evidence":evidence})
    if not accepted:
        return technical_miss(interpretation, unknown=unknown, evidence=evidence)
    from app.catalog.index.catalog_index import build_allowed_id_sets, index_products_best_effort
    accepted = apply_persona_presentation_order(accepted)[:customer_result_limit()]
    index_products_best_effort(accepted, factual_source="tray_live")
    return AgentResult(reply_text=confirmed_product_reply(accepted, interpretation), intent="commerce",
        commercial_data={"products":accepted}, response_metadata={
            "presented_products":True, "product_resolution_state":"options_presented",
            "technical_requirements":required, "technical_evidence":evidence,
            "allowed_id_sets":{k:sorted(v) for k,v in build_allowed_id_sets(accepted).items()},
            "factual_fallback_text":confirmed_product_reply(accepted, interpretation),
        })
