from __future__ import annotations

from typing import Protocol


class StoreKnowledgeProvider(Protocol):
    """Internal seam for future official NewStore policy/institutional knowledge."""

    def lookup(self, question: str) -> str | None:
        ...


class EmptyStoreKnowledgeProvider:
    def lookup(self, question: str) -> str | None:
        return None
