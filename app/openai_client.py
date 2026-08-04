"""Shared OpenAI clients for the agent runtime.

Business modules should obtain clients from here (or via openai_gateway)
instead of constructing AsyncOpenAI/OpenAI on every call.
"""

from __future__ import annotations

from openai import AsyncOpenAI, OpenAI

from .config import get_settings

_async_client: AsyncOpenAI | None = None
_sync_client: OpenAI | None = None


def get_async_openai_client(*, api_key: str | None = None) -> AsyncOpenAI:
    """Return a process-wide AsyncOpenAI client (recreated if key changes)."""
    global _async_client
    settings = get_settings()
    key = (api_key if api_key is not None else settings.openai_api_key) or ""
    if _async_client is None or getattr(_async_client, "api_key", None) != key:
        _async_client = AsyncOpenAI(api_key=key)
    return _async_client


def get_sync_openai_client(*, api_key: str | None = None) -> OpenAI:
    """Return a process-wide sync OpenAI client (audio/embeddings helpers)."""
    global _sync_client
    settings = get_settings()
    key = (api_key if api_key is not None else settings.openai_api_key) or ""
    if _sync_client is None or getattr(_sync_client, "api_key", None) != key:
        _sync_client = OpenAI(api_key=key)
    return _sync_client


def reset_openai_clients() -> None:
    """Clear cached clients (tests / settings reload)."""
    global _async_client, _sync_client
    _async_client = None
    _sync_client = None
