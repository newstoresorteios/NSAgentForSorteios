"""Helpers to mock OpenAI structured parse after the gateway migration."""

from __future__ import annotations

from typing import Any


_LEGACY_ASYNC_OPENAI_TARGETS = (
    "app.sales_agent.AsyncOpenAI",
    "app.openai_agent.AsyncOpenAI",
    "app.verify.response_critique.AsyncOpenAI",
    "app.catalog.product_image_index.AsyncOpenAI",
    "app.catalog.image_product_id.AsyncOpenAI",
    "app.catalog.product_retrieval.AsyncOpenAI",
    "app.catalog.category_resolver.AsyncOpenAI",
    "app.verify.quality_judge.AsyncOpenAI",
    "app.commerce.checkout_data_service.AsyncOpenAI",
)


def install_fake_openai_client(monkeypatch: Any, fake_client: Any) -> None:
    """Route ChatCompletionsGateway + legacy AsyncOpenAI() sites to a fake client."""
    from app.llm.openai_client import reset_openai_clients
    from app.llm.openai_gateway import reset_openai_gateway

    reset_openai_clients()
    reset_openai_gateway()

    def _factory(**_kwargs: Any) -> Any:
        if isinstance(fake_client, type):
            return fake_client(**_kwargs)
        return fake_client

    monkeypatch.setattr("app.llm.openai_client.get_async_openai_client", _factory)
    monkeypatch.setattr("app.llm.openai_gateway.get_async_openai_client", _factory)
    monkeypatch.setattr("app.llm.openai_client.get_sync_openai_client", _factory)

    # Legacy modules that still expose AsyncOpenAI for older tests.
    ctor = fake_client if isinstance(fake_client, type) else (lambda **_k: fake_client)
    for target in _LEGACY_ASYNC_OPENAI_TARGETS:
        try:
            monkeypatch.setattr(target, ctor)
        except Exception:
            # Symbol may have been removed after gateway migration.
            pass
