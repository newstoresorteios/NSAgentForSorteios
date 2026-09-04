"""Sales facade: bind turn contract and Tray query authority, then run catalog retrieval."""

from __future__ import annotations

from app.catalog.retrieval.executor import (
    execute_contextual_product_lookup as _run_contextual,
    _execute_compiled_product_retrieval_unlocked,
)
from app.catalog.retrieval.limits import ToolExecutor
from app.catalog.retrieval.ports import bind_execute_tool, reset_execute_tool, resolve_execute_tool
from app.catalog.retrieval.rerank import rerank_products
from app.catalog.retrieval.runtime import get_settings
from app.models import AgentResult, SalesInterpretation


async def execute_contextual_product_lookup(
    interpretation: SalesInterpretation,
    product_reference,
    *,
    execute_tool: ToolExecutor | None = None,
) -> AgentResult:
    tool = resolve_execute_tool(execute_tool)
    token = bind_execute_tool(tool)
    try:
        return await _run_contextual(
            interpretation,
            product_reference,
            execute_tool=tool,
        )
    finally:
        reset_execute_tool(token)


async def execute_compiled_product_retrieval(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
    commerce_state=None,
    execute_tool: ToolExecutor | None = None,
) -> AgentResult | None:
    from app.sales.answer_council import apply_turn_contract_for_search
    from app.sales.tray_query_authority import (
        authorize_catalog_search,
        bind_catalog_authorization,
        budget_hard_miss_result,
        reset_catalog_authorization,
    )
    from app.sales.tray_refresh import (
        constraint_requires_tray_refresh,
        tray_list_query_extras,
    )

    tool = resolve_execute_tool(execute_tool)
    tool_token = bind_execute_tool(tool)
    bound = apply_turn_contract_for_search(
        interpretation,
        message_text=message_text,
        commerce_state=commerce_state,
    )
    catalog_authorization = authorize_catalog_search(bound)
    auth_token = bind_catalog_authorization(catalog_authorization)
    try:
        return await _execute_compiled_product_retrieval_unlocked(
            bound,
            message_text=message_text,
            execute_tool=tool,
            list_query_extras=tray_list_query_extras,
            requires_tray_refresh=constraint_requires_tray_refresh,
            budget_hard_miss=lambda interp, candidates: budget_hard_miss_result(
                interp,
                candidates,
                authorization=catalog_authorization,
            ),
        )
    finally:
        reset_catalog_authorization(auth_token)
        reset_execute_tool(tool_token)


_execute_compiled_product_retrieval = execute_compiled_product_retrieval
_execute_contextual_product_lookup = execute_contextual_product_lookup

__all__ = [
    "execute_compiled_product_retrieval",
    "execute_contextual_product_lookup",
    "get_settings",
    "rerank_products",
    "_execute_compiled_product_retrieval",
    "_execute_compiled_product_retrieval_unlocked",
    "_execute_contextual_product_lookup",
]
