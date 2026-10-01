"""Resolve an explicit workspace without guessing ownership of legacy memory."""
from __future__ import annotations

from uuid import UUID


def memory_workspace(workspace_id: str | None = None, *, required: bool = False) -> str | None:
    from app.persona.persona_runtime import get_persona_runtime

    runtime = get_persona_runtime()
    current = getattr(runtime, "workspace_id", None)
    explicit = str(UUID(str(workspace_id))) if workspace_id else None
    bound = str(UUID(str(current))) if current else None
    if explicit and bound and explicit != bound:
        raise ValueError("memory_workspace_mismatch")
    resolved = explicit or bound
    if required and not resolved:
        raise ValueError("memory_workspace_required")
    return resolved
