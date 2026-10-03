from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import BadRequestError

from app.direct.agent import DirectOpenAIAgent
from app.direct.diagnostics import safe_error_details


def api_error(**body):
    return BadRequestError("private customer text sk-secret", response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.openai.com/v1/conversations"),
        headers={"x-request-id": "req_0123456789abcdef0123456789abcdef"}), body=body)


def agent():
    api = NS(conversations=NS(create=AsyncMock(return_value=NS(id="conv-test")),
                             items=NS(create=AsyncMock())))
    api.with_options = lambda **_: api
    return DirectOpenAIAgent(api, NS(direct_timeout_seconds=30)), api


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 20, 21, 40, 41, 60, 70])
async def test_history_seed_respects_api_limit_and_preserves_order(count):
    instance, api = agent()
    delivered = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
                 for i in range(count)]
    received = []

    async def create(*args, items):
        assert len(items) <= 20
        received.extend(items)
        return NS(id="conv-test")

    api.conversations.create.side_effect = create
    api.conversations.items.create.side_effect = create
    assert await instance._conversation(scope="test", history=delivered, previous={}) == ("conv-test", 0)
    assert received == delivered[-60:]
    for call in api.conversations.items.create.await_args_list:
        assert call.args == ("conv-test",)


@pytest.mark.asyncio
async def test_failed_append_aborts_seed_and_logs_only_safe_metadata(monkeypatch):
    instance, api = agent()
    error = api_error(code="array_above_max_length", param="items")
    api.conversations.items.create.side_effect = error
    events = []
    monkeypatch.setattr("app.ops.observability.log_event", lambda *args: events.append(args))
    with pytest.raises(BadRequestError):
        await instance._conversation(scope="test", previous={}, history=[
            {"role": "user", "content": "private customer text"} for _ in range(60)])
    api.conversations.items.create.assert_awaited_once()
    event, details = events[0]
    assert event == "direct.conversation.failed"
    assert details["operation"] == "conversations.items.create"
    assert details["history_item_count"] == 60
    assert details["batch_item_count"] == 20
    assert details["status_code"] == 400
    assert details["error_param"] == "items"
    assert "private customer" not in str(events)
    assert "sk-secret" not in str(events)


def test_diagnostics_drop_untrusted_fields():
    error = api_error(code={"secret": "sk-secret"}, param="private customer text")
    error.request_id = "sk-secret"
    assert safe_error_details(error) == {"error_type": "BadRequestError", "status_code": 400}
    assert safe_error_details(RuntimeError("sk-secret")) == {"error_type": "RuntimeError"}
