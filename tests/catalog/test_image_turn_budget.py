from types import SimpleNamespace

import pytest

from app.models import AgentResult, IncomingMessage
from app.ops.runtime_context import reset_current_turn, set_current_turn
from app.ops.turn_runtime import LLMCallBudget, TurnRuntimeContext


@pytest.mark.asyncio
async def test_image_route_promotes_turn_to_complex_budget(monkeypatch):
    from app.agents import door
    from app.agents.door_media import try_media_routes

    incoming = IncomingMessage(
        channel="whatsapp",
        text="quero esse relógio",
        input_modality="text_with_image",
        attachment_type="image",
        image_url="https://example.com/sealander.jpg",
    )

    async def image_result(_message):
        return AgentResult(
            reply_text="Pela foto, parece um Christopher Ward Sealander.",
            intent="commerce",
            response_metadata={"response_source": "image_vision"},
        )

    monkeypatch.setattr(door, "image_search_eligible", lambda _message: True)
    monkeypatch.setattr(door, "handle_image_product_search", image_result)

    runtime = TurnRuntimeContext(
        trace_id="image-budget",
        llm_budget=LLMCallBudget(max_calls=2, enforce=True),
    )
    token = set_current_turn(runtime)
    try:
        result = await try_media_routes(incoming, SimpleNamespace(active_product=None))
    finally:
        reset_current_turn(token)

    assert result is not None
    assert runtime.execution_path == "complex"
    assert runtime.llm_budget.max_calls >= 4
