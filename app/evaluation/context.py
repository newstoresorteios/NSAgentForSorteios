from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationContext:
    workspace_id: str
    persona: Any
    history: list[dict] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    fixtures: dict[str, dict] | None = None
    captured: dict[str, dict] = field(default_factory=dict)
    max_tool_calls: int = 30
    started_tool_calls: int = 0
    review_inputs: list[dict] = field(default_factory=list)


_context: ContextVar[EvaluationContext | None] = ContextVar("evaluation_context", default=None)


def current_evaluation() -> EvaluationContext | None:
    return _context.get()


def bind_evaluation(context: EvaluationContext):
    return _context.set(context)


def reset_evaluation(token):
    _context.reset(token)


def prohibit_side_effect(operation: str) -> None:
    context = current_evaluation()
    if context is not None:
        context.blocked.append(operation)
        raise RuntimeError("evaluation_side_effect_prohibited:" + operation)
