from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .runtime_context import get_current_turn


T = TypeVar("T")


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    input_tokens = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    output_tokens = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    return int(input_tokens or 0), int(output_tokens or 0)


def _start_call(call_type: str) -> tuple[Any, float]:
    context = get_current_turn()
    if context is not None:
        context.register_openai_call(call_type)
    return context, time.perf_counter()


def _finish_call(context: Any, started_at: float, response: Any) -> None:
    if context is None:
        return
    input_tokens, output_tokens = _usage_tokens(response)
    context.register_openai_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    stage_name = f"openai_{context.openai_call_count}"
    context.stage_durations_ms[stage_name] = round(
        (time.perf_counter() - started_at) * 1000,
        2,
    )


async def execute_openai_call(
    *,
    call_type: str,
    operation: Callable[[], Awaitable[T]],
    timeout_seconds: float | None = None,
) -> T:
    context, started_at = _start_call(call_type)
    try:
        awaitable = operation()
        response = (
            await asyncio.wait_for(awaitable, timeout=timeout_seconds)
            if timeout_seconds and timeout_seconds > 0
            else await awaitable
        )
    except Exception:
        if context is not None:
            context.register_integration_failure("openai")
        raise
    _finish_call(context, started_at, response)
    return response


def execute_openai_call_sync(
    *,
    call_type: str,
    operation: Callable[[], T],
) -> T:
    context, started_at = _start_call(call_type)
    try:
        response = operation()
    except Exception:
        if context is not None:
            context.register_integration_failure("openai")
        raise
    _finish_call(context, started_at, response)
    return response
