"""Ports so catalog retrieval does not import sales_agent or sales workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from app.catalog.retrieval.limits import ToolExecutor
from app.models import AgentResult, SalesInterpretation

ListQueryExtras = Callable[[SalesInterpretation | None], dict[str, Any]]
RequiresTrayRefresh = Callable[[SalesInterpretation | None, str | None], bool]
BudgetHardMiss = Callable[
    [SalesInterpretation, list[dict[str, Any]]],
    AgentResult | None,
]
CompiledRetrieval = Callable[..., Awaitable[AgentResult | None]]

_execute_tool: ContextVar[ToolExecutor | None] = ContextVar(
    "catalog_execute_tool",
    default=None,
)
_compiled_retrieval: ContextVar[CompiledRetrieval | None] = ContextVar(
    "catalog_compiled_retrieval",
    default=None,
)


def bind_execute_tool(execute_tool: ToolExecutor):
    return _execute_tool.set(execute_tool)


def reset_execute_tool(token) -> None:
    _execute_tool.reset(token)


def resolve_execute_tool(explicit: ToolExecutor | None = None) -> ToolExecutor:
    """Use the injected tool, else the test monkeypatch on sales_agent, else Tray."""
    if explicit is not None:
        return explicit
    bound = _execute_tool.get()
    if bound is not None:
        return bound
    import sys

    sales_agent = sys.modules.get("app.sales_agent")
    if sales_agent is not None:
        fn = getattr(sales_agent, "execute_tool", None)
        if fn is not None:
            return fn
    from app.tray.tray_tools import execute_tool

    return execute_tool


def bind_compiled_retrieval(retrieve: CompiledRetrieval):
    return _compiled_retrieval.set(retrieve)


def reset_compiled_retrieval(token) -> None:
    _compiled_retrieval.reset(token)


def resolve_compiled_retrieval(
    explicit: CompiledRetrieval | None = None,
) -> CompiledRetrieval:
    """Use the injected retrieve, else sales_agent monkeypatch, else catalog executor."""
    if explicit is not None:
        return explicit
    bound = _compiled_retrieval.get()
    if bound is not None:
        return bound
    import sys

    sales_agent = sys.modules.get("app.sales_agent")
    if sales_agent is not None:
        fn = getattr(sales_agent, "_execute_compiled_product_retrieval", None)
        if fn is not None:
            return fn
    lookup = sys.modules.get("app.sales.product_lookup")
    if lookup is not None:
        fn = getattr(lookup, "execute_compiled_product_retrieval", None)
        if fn is not None:
            return fn

    async def _unlocked(interpretation: SalesInterpretation, **_kwargs):
        from app.catalog.retrieval.executor import (
            _execute_compiled_product_retrieval_unlocked,
        )

        return await _execute_compiled_product_retrieval_unlocked(
            interpretation,
            execute_tool=resolve_execute_tool(),
        )

    return _unlocked


async def call_execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    tool = resolve_execute_tool()
    return await tool(name, arguments)


def default_list_query_extras(_interpretation: SalesInterpretation | None) -> dict[str, Any]:
    return {}


def default_requires_tray_refresh(
    _interpretation: SalesInterpretation | None,
    _message_text: str | None,
) -> bool:
    return False


def default_budget_hard_miss(
    _interpretation: SalesInterpretation,
    _candidates: list[dict[str, Any]],
) -> AgentResult | None:
    return None
