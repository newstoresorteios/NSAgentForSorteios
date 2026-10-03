import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openai import AsyncOpenAI

from app.config import Settings
from app.direct.agent import DirectOpenAIAgent
from app.direct.tools import DirectTools
from app.models import IncomingMessage
from app.tray.tray_adapter_client import TrayAdapterClient
from app.tray.tray_circuit_breaker import reset_tray_circuit_breaker_for_tests


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    reset_tray_circuit_breaker_for_tests()
    monkeypatch.setattr("app.catalog.index.snapshot.product_cache_enabled", lambda: False)
    yield
    reset_tray_circuit_breaker_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,path,payload", [
    ("search_products", {"query": "Seiko", "brand": "Seiko", "max_price": 3000, "ready_stock": False},
     "/internal/products", {"products": [{"id": "42", "name": "Seiko", "current_price": 2500}]}),
    ("get_product", {"product_id": "42"}, "/internal/products/42", {"product": {"id": "42", "current_price": 2500}}),
    ("check_inventory", {"product_id": "42"}, "/internal/products/42/stock", {"product_id": "42", "stock": 1, "available": True})])
async def test_internal_http_contract(tool, args, path, payload):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        executor = DirectTools(incoming=IncomingMessage(), history=[], documents=[], adapter=adapter)
        result = await executor.execute(tool, json.dumps(args))
    request = seen[0]
    assert request.method == "GET" and request.url.path == path
    assert request.headers["authorization"] == "Bearer private-token"
    assert request.content == b""
    assert request.extensions["timeout"]["read"] == 12
    if tool == "search_products":
        assert dict(request.url.params) == {"name": "Seiko", "brand": "Seiko", "available": "true",
                                           "limit": "5", "current_price_range": "0,3000"}
    else:
        assert not request.url.query
    assert result == {"ok": True, "source": "tray_adapter", "data": payload}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 404, 503, "timeout"])
async def test_transport_errors_are_normalized(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret upstream", request=request)
        return httpx.Response(failure, json={"error": "secret upstream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        executor = DirectTools(incoming=IncomingMessage(), history=[], documents=[], adapter=adapter)
        result = await executor.execute("get_product", '{"product_id":"42"}')
    assert result["ok"] is False and result["error"] == "commerce_unavailable"
    assert "secret upstream" not in json.dumps(result)


@pytest.mark.asyncio
async def test_real_sdk_responses_and_conversations_wire_format():
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.headers["authorization"] == "Bearer test-key"
        if request.url.path == "/v1/conversations":
            return httpx.Response(200, json={"id": "conv_test", "object": "conversation", "created_at": 1, "metadata": {}})
        assert request.url.path == "/v1/responses"
        assert body["conversation"] == "conv_test"
        assert body["input"][0]["content"][0]["type"] == "input_text"
        assert "previous_response_id" not in body
        return httpx.Response(200, json={"id": "resp_test", "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-4.1-mini", "output": [{"type": "message", "id": "msg_test",
              "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Olá!", "annotations": []}]}],
            "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        api = AsyncOpenAI(api_key="test-key", http_client=http)
        msg = IncomingMessage(text="Oi", sender_key="a", conversation_id="b")
        tools = DirectTools(incoming=msg, history=[], documents=[])
        result = await DirectOpenAIAgent(api, Settings(_env_file=None)).run_turn(
            incoming=msg, workspace_id="w", history=[], previous={}, tools=tools,
            content=[{"type": "input_text", "text": "Oi"}])
    assert result.reply_text == "Olá!"
    assert [p for p, _ in requests] == ["/v1/conversations", "/v1/responses"]


@pytest.mark.asyncio
async def test_admin_preview_requires_auth_and_never_sends(monkeypatch):
    import app.direct.admin as admin
    import app.core.security as security
    cfg = Settings(_env_file=None, ADMIN_API_TOKEN="test-secret")
    monkeypatch.setattr(security, "get_settings", lambda: cfg)
    preview = AsyncMock(return_value={"ok": True, "sent_to_customer": False, "reply_text": "Olá"})
    monkeypatch.setattr(admin, "preview_turn", preview)
    app = FastAPI()
    app.include_router(admin.router)
    payload = {"workspace_id": "00000000-0000-0000-0000-000000000001", "text": "Olá"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.post("/api/test/direct", json=payload)).status_code == 401
        preview.assert_not_awaited()
        good = await http.post("/api/test/direct", json=payload, headers={"Authorization": "Bearer test-secret"})
        assert good.status_code == 200 and good.json()["sent_to_customer"] is False


@pytest.mark.asyncio
async def test_image_download_failure_is_explicit(monkeypatch):
    from app.direct.pipeline import input_content
    monkeypatch.setattr("app.core.remote_media.download_trusted_media", AsyncMock(side_effect=ValueError("blocked")))
    monkeypatch.setattr("app.direct.media.download_story_media_file", AsyncMock(side_effect=ValueError("blocked")))
    content = await input_content(IncomingMessage(text="Quanto custa?", image_url="http://127.0.0.1"))
    assert "Mídia indisponível" in content[-1]["text"]


def test_history_query_has_all_identity_boundaries(monkeypatch):
    from app.direct.history import load_history
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args): self.sql, self.args = sql, args
        def fetchall(self): return [{"id": 1, "text": "Olá", "reply_text": "Oi", "metadata": {"engine": "direct"}}]
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return cursor
    cursor = Cursor()
    monkeypatch.setattr("app.direct.history.get_conn", lambda: Conn())
    msg = IncomingMessage(provider="meta", channel="instagram", sender_key="ig:a", conversation_id="c", raw={"inbound_id": 2})
    history, previous = load_history(msg, "00000000-0000-0000-0000-000000000001")
    assert cursor.args["before"] == 2 and cursor.args["identity"] == "ig:a"
    for bound in ["workspace_id", "conversation_id", "i.channel", "i.provider", "i.sender_key", "provider_send_ok=true"]:
        assert bound in cursor.sql
    assert history[-1]["content"] == "Oi" and history[-1]["metadata"]["engine"] == "direct"
    assert previous["engine"] == "direct"
