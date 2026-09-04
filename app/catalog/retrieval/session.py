"""Mutable candidate pool for one compiled retrieval turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.catalog.retrieval.hard_filter import hard_filter_products
from app.catalog.retrieval.limits import ToolExecutor
from app.catalog.retrieval.ports import (
    BudgetHardMiss,
    ListQueryExtras,
    RequiresTrayRefresh,
    default_budget_hard_miss,
    default_list_query_extras,
    default_requires_tray_refresh,
)
from app.catalog.retrieval.scoring import exact_progress_matches
from app.catalog.retrieval.tokens import (
    preference_color_tokens,
    product_matches_color_tokens,
)
from app.models import SalesInterpretation


@dataclass
class RetrievalSession:
    interpretation: SalesInterpretation
    retrieval_plan: Any
    message_text: str | None
    execute_tool: ToolExecutor
    has_budget: bool = False
    category_resolution: Any = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    hard_filtered: list[dict[str, Any]] = field(default_factory=list)
    excluded_ids: set[str] = field(default_factory=set)
    product_lookup_failed: bool = False
    catalog_probe_ok: bool = False
    specific_resolution: Any = None
    used_brand_candidates: bool = False
    used_category_candidates: bool = False
    catalog_index_primary: bool = False
    catalog_index_strategy: str | None = None
    catalog_index_seeded: int = 0
    catalog_discovered_count: int = 0
    list_query_extras: ListQueryExtras = default_list_query_extras
    requires_tray_refresh: RequiresTrayRefresh = default_requires_tray_refresh
    budget_hard_miss: BudgetHardMiss = default_budget_hard_miss

    def accumulation_limit(self) -> int:
        if self.retrieval_plan.mode == "exact":
            return (
                self.retrieval_plan.discovery_max_products
                + self.retrieval_plan.candidate_limit
            )
        return self.retrieval_plan.candidate_limit

    def absorb_products(
        self,
        raw_products: list[Any],
        *,
        catalog_discovery: bool = False,
        prefer_color: bool = False,
    ) -> None:
        color_tokens = preference_color_tokens(self.interpretation)
        for product in raw_products:
            if not isinstance(product, dict) or product.get("id") is None:
                continue
            product_id = str(product["id"])
            if product_id in self.seen_ids or product_id in self.excluded_ids:
                continue
            is_color_hit = bool(
                color_tokens
                and product_matches_color_tokens(product, color_tokens)
            )
            at_limit = len(self.candidates) >= self.accumulation_limit()
            if at_limit and not (prefer_color and is_color_hit):
                continue
            if at_limit and prefer_color and is_color_hit:
                for index, existing in enumerate(self.candidates):
                    if not product_matches_color_tokens(existing, color_tokens):
                        evicted_id = str(existing.get("id"))
                        self.candidates.pop(index)
                        self.seen_ids.discard(evicted_id)
                        break
                else:
                    continue
            self.seen_ids.add(product_id)
            self.candidates.append(product)
            if catalog_discovery:
                self.catalog_discovered_count += 1

    def refresh_hard_filtered(self) -> None:
        if self.retrieval_plan.mode == "exact":
            self.hard_filtered = exact_progress_matches(
                self.candidates,
                self.interpretation,
            )
        else:
            self.hard_filtered = hard_filter_products(
                self.candidates,
                self.interpretation,
                mode=self.retrieval_plan.mode,
            )

    async def search_products(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.execute_tool("search_products", arguments)
