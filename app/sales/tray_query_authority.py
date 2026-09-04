"""Catalog query authority — consult Tray contract, then allow or block a search.

This is the analysis agent that must run before a catalog query. It does not
call developers.tray.com.br on the WhatsApp turn. It consults the pinned
contract, binds hard constraints from the conversation (brand + budget), and
forbids the "mais próximos" near-match from presenting items over budget.

Empty authorized search → honest miss, not a luxury-brand dump.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from ..models import AgentResult, SalesInterpretation
from app.catalog.product_retrieval import effective_price, hard_filter_products
from .tray_capability_contract import (
    consult_tray_list_products_contract,
    contract_as_log,
)


EmptyOutcome = Literal["present", "honest_constraint_miss", "near_match_ok"]

_active_authorization: ContextVar["QueryAuthorization | None"] = ContextVar(
    "tray_query_authorization",
    default=None,
)


@dataclass(frozen=True)
class QueryAuthorization:
    allowed: bool
    reason: str
    tool: str
    brand: str | None
    budget_min: float | None
    budget_max: float | None
    empty_outcome: EmptyOutcome
    forbid_near_match: bool = False
    contract: dict[str, Any] = field(default_factory=dict)

    def log_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "tool": self.tool,
            "brand": self.brand,
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "empty_outcome": self.empty_outcome,
            "forbid_near_match": self.forbid_near_match,
            "contract": self.contract,
        }


def current_catalog_authorization() -> QueryAuthorization | None:
    return _active_authorization.get()


def bind_catalog_authorization(authorization: QueryAuthorization):
    return _active_authorization.set(authorization)


def reset_catalog_authorization(token) -> None:
    _active_authorization.reset(token)


def authorize_catalog_search(
    interpretation: SalesInterpretation,
    *,
    tool: str = "search_products",
) -> QueryAuthorization:
    """Consult Tray list-products contract, then authorize the search plan."""
    contract = consult_tray_list_products_contract()
    prefs = interpretation.preferences
    brand = (interpretation.subject.brand or "").strip() or None
    has_budget = prefs.budget_max is not None or prefs.budget_min is not None
    forbid_near_match = bool(getattr(interpretation, "_forbid_near_match", False))
    empty_outcome: EmptyOutcome = (
        "honest_constraint_miss"
        if has_budget or forbid_near_match
        else "near_match_ok"
    )
    authorization = QueryAuthorization(
        allowed=True,
        reason="tray_contract_consulted",
        tool=tool,
        brand=brand,
        budget_min=prefs.budget_min,
        budget_max=prefs.budget_max,
        empty_outcome=empty_outcome,
        forbid_near_match=forbid_near_match,
        contract=contract_as_log(contract),
    )
    print("[sales.tray_query.authority]", authorization.log_payload())
    return authorization


def _brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _miss_subject_label(brand: str | None) -> str:
    cleaned = str(brand or "").strip()
    return cleaned if cleaned else "relógios"


def _budget_miss_reply(brand: str | None, ceiling: str, floor: float | None) -> str:
    subject = _miss_subject_label(brand)
    if floor is not None:
        if brand:
            return (
                f"Não encontrei {subject} até {ceiling}. "
                f"Na loja, o {subject} mais acessível que vi fica a partir de "
                f"{_brl(floor)} — fora da faixa. "
                "Prefere outra marca nessa faixa, ou subir o orçamento?"
            )
        return (
            f"Não encontrei {subject} até {ceiling}. "
            f"Na loja, o mais acessível que vi fica a partir de "
            f"{_brl(floor)} — fora da faixa. "
            "Quer ajustar a faixa ou outro critério?"
        )
    if brand:
        return (
            f"Não encontrei {subject} até {ceiling}. "
            "Prefere outra marca nessa faixa, ou subir o orçamento?"
        )
    return (
        f"Não encontrei {subject} até {ceiling}. "
        "Quer ajustar a faixa ou outro critério?"
    )


def _products_passing_without_budget(
    candidates: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    prefs = interpretation.preferences
    if prefs.budget_max is None and prefs.budget_min is None:
        return []
    relaxed = interpretation.model_copy(
        update={
            "preferences": prefs.model_copy(
                update={"budget_max": None, "budget_min": None}
            )
        }
    )
    return hard_filter_products(candidates, relaxed, mode="recommendation")


def cheapest_over_budget_floor(
    products: list[dict[str, Any]],
    *,
    budget_max: float | None,
) -> float | None:
    prices: list[float] = []
    for product in products:
        price = effective_price(product)
        if price is None:
            continue
        if budget_max is not None and price <= budget_max:
            continue
        prices.append(price)
    return min(prices) if prices else None


def products_within_authorization_budget(
    products: list[dict[str, Any]],
    authorization: QueryAuthorization,
) -> list[dict[str, Any]]:
    if authorization.budget_max is None and authorization.budget_min is None:
        return [item for item in products if isinstance(item, dict)]
    selected: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        price = effective_price(product)
        if authorization.budget_min is not None and (
            price is None or price < authorization.budget_min
        ):
            continue
        if authorization.budget_max is not None and (
            price is None or price > authorization.budget_max
        ):
            continue
        selected.append(product)
    return selected


def budget_miss_from_authorization(
    authorization: QueryAuthorization,
    candidates: list[dict[str, Any]],
) -> AgentResult:
    """Honest miss when near-match is forbidden or the teto emptied the pool."""
    floor = cheapest_over_budget_floor(
        [item for item in candidates if isinstance(item, dict)],
        budget_max=authorization.budget_max,
    )
    brand = authorization.brand
    if authorization.budget_max is not None:
        reply = _budget_miss_reply(brand, _brl(authorization.budget_max), floor)
    else:
        reply = (
            f"Não encontrei {_miss_subject_label(brand)} dentro do que você pediu. "
            "Prefere ajustar marca ou faixa?"
        )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="recommendation_budget_miss",
        commercial_data={
            "products": [],
            "match_status": "budget_miss",
            "brand": authorization.brand,
            "budget_max": authorization.budget_max,
            "budget_min": authorization.budget_min,
            "brand_floor_price": floor,
        },
        response_metadata={
            "presented_products": False,
            "product_resolution_state": "needs_clarification",
            "clear_active_product": True,
            "guided_near_match": False,
            "tray_query_authorized": True,
            "hard_budget_max": authorization.budget_max,
            "constraint_miss": "budget",
            "domain": "commerce",
            "forbid_near_match": True,
        },
    )


def budget_hard_miss_result(
    interpretation: SalesInterpretation,
    candidates: list[dict[str, Any]],
    *,
    authorization: QueryAuthorization | None = None,
) -> AgentResult | None:
    """If budget is known and nothing survives it, do not near-match over budget."""
    auth = authorization or authorize_catalog_search(interpretation)
    if auth.empty_outcome != "honest_constraint_miss":
        return None
    if auth.budget_max is None and auth.budget_min is None:
        return None

    in_budget = hard_filter_products(
        candidates,
        interpretation,
        mode="recommendation",
    )
    if in_budget:
        return None

    outside = _products_passing_without_budget(candidates, interpretation)
    floor = cheapest_over_budget_floor(outside, budget_max=auth.budget_max)
    brand = auth.brand
    if auth.budget_max is not None:
        reply = _budget_miss_reply(brand, _brl(auth.budget_max), floor)
    else:
        reply = (
            f"Não encontrei {_miss_subject_label(brand)} dentro da faixa pedida. "
            "Prefere outra marca, ou ajustar o orçamento?"
        )
    print(
        "[sales.tray_query.budget_miss]",
        {
            "brand": auth.brand,
            "budget_max": auth.budget_max,
            "candidates": len(candidates),
            "outside_brand_hits": len(outside),
            "floor": floor,
        },
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason="recommendation_budget_miss",
        commercial_data={
            "products": [],
            "match_status": "budget_miss",
            "brand": auth.brand,
            "budget_max": auth.budget_max,
            "budget_min": auth.budget_min,
            "brand_floor_price": floor,
        },
        response_metadata={
            "presented_products": False,
            "product_resolution_state": "needs_clarification",
            "clear_active_product": True,
            "guided_near_match": False,
            "tray_query_authorized": True,
            "hard_budget_max": auth.budget_max,
            "constraint_miss": "budget",
            "domain": "commerce",
        },
    )
