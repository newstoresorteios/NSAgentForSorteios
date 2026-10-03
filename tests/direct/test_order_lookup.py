import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from app.direct.tools import DirectTools
from app.models import IncomingMessage
from app.tray.tray_adapter_client import TrayAdapterClient
from app.tray.tray_circuit_breaker import reset_tray_circuit_breaker_for_tests

CPF = "52998224725"
PHONE = "5521988887777"
ORDER = {
    "id": "26116",
    "order_id": "26116",
    "status": "Enviado",
    "status_group": "shipped",
    "customer": {"name": "Titular Secreto", "phone": "21988887777", "address": "Rua Secreta 10"},
    "estimated_delivery_date": "2026-10-10",
    "sending_code": "BR123456789BR",
    "shipment": {"address": "Rua Secreta 10"},
}


@pytest.fixture(autouse=True)
def isolation():
    reset_tray_circuit_breaker_for_tests()
    yield
    reset_tray_circuit_breaker_for_tests()


def message(text, **kwargs):
    return IncomingMessage(text=text, provider="brevo", channel="whatsapp",
                           sender_phone=PHONE, sender_key=PHONE, conversation_id="thread", **kwargs)


def tools(text, adapter=None, history=None, continuity=None):
    executor = DirectTools(incoming=message(text), history=history or [], documents=[], adapter=adapter)
    if continuity:
        executor.continuity.update(continuity)
    return executor


async def execute(executor, **fields):
    payload = {"order_reference": None, "document": None, **fields}
    return await executor.execute("lookup_order", json.dumps(payload))


@pytest.mark.asyncio
async def test_guessed_order_number_never_reaches_the_adapter():
    adapter = NS(get_order_complete=AsyncMock(side_effect=AssertionError("lookup")))
    result = await execute(tools("Prazo do meu pedido efetuado", adapter), order_reference="26116")
    assert result["error"] == "identifier_not_in_customer_message"
    adapter.get_order_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_absent_from_the_customer_text_is_rejected():
    adapter = NS(list_customers=AsyncMock(side_effect=AssertionError("lookup")))
    result = await execute(tools("Qual o prazo do pedido 26116?", adapter), document=CPF)
    assert result["error"] == "document_not_in_customer_message"
    adapter.list_customers.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_match_returns_only_status_deadline_and_tracking():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"order": ORDER})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        executor = tools("Qual o prazo do pedido 26116?", adapter)
        result = await execute(executor, order_reference="26116")
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/internal/orders/26116/complete"
    assert request.headers["authorization"] == "Bearer private-token"
    assert request.content == b""
    assert request.extensions["timeout"]["read"] == 12
    assert result["ok"] is True
    assert result["status"] == "Enviado"
    assert result["estimated_delivery_date"] == "2026-10-10"
    assert result["sending_code"] == "BR123456789BR"
    encoded = json.dumps(result)
    assert "Rua Secreta" not in encoded and "Titular Secreto" not in encoded
    assert executor.continuity["order_lookup"]["order_reference"] == "26116"


@pytest.mark.asyncio
async def test_phone_mismatch_hides_the_order():
    def respond(request):
        foreign = {**ORDER, "customer": {"phone": "11977776666", "name": "Outra Pessoa", "address": "Rua Oculta"}}
        return httpx.Response(200, json={"order": foreign, "secret": "token interno"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        executor = tools("Quero o status do pedido 26116", adapter)
        result = await execute(executor, order_reference="26116")
    assert result["ok"] is False and result["error"] == "owner_not_confirmed"
    encoded = json.dumps(result)
    assert "Enviado" not in encoded and "Rua Oculta" not in encoded and "token interno" not in encoded
    assert executor.continuity["order_lookup"]["order_reference"] == "26116"


@pytest.mark.asyncio
async def test_customer_document_uses_the_internal_read_contract():
    seen = []

    def respond(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/internal/customers":
            return httpx.Response(200, json={"customers": [{"id": "9", "name": "Titular Secreto"}]})
        if request.url.path == "/internal/orders":
            return httpx.Response(200, json={"orders": [{
                "order_id": "26116", "id": "26116", "customer_id": "9", "status": "Pago"}]})
        return httpx.Response(200, json={"order": ORDER})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        result = await execute(tools(f"Meu CPF é {CPF}", adapter), document=CPF)
    assert seen == [
        ("GET", "/internal/customers", {"cpf": CPF, "limit": "5"}),
        ("GET", "/internal/orders", {"customer_id": "9"}),
        ("GET", "/internal/orders/26116/complete", {}),
    ]
    assert result["ok"] is True and result["order_id"] == "26116"
    assert "Titular Secreto" not in json.dumps(result)


@pytest.mark.asyncio
async def test_document_lookup_accepts_customer_scoped_order_without_customer_id():
    def respond(request):
        if request.url.path == "/internal/customers":
            return httpx.Response(200, json={"customers": [{"id": "9"}]})
        if request.url.path == "/internal/orders":
            return httpx.Response(200, json={"orders": [{"id": "26116", "status": "ENVIADO"}]})
        return httpx.Response(200, json={"order": {**ORDER, "customer_id": "9"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        result = await execute(tools(f"pedido 26116 CPF {CPF}", adapter), order_reference="26116", document=CPF)
    assert result["ok"] is True and result["status"] == "Enviado"


@pytest.mark.asyncio
async def test_several_orders_do_not_leak_their_numbers():
    def respond(request):
        if request.url.path == "/internal/customers":
            return httpx.Response(200, json={"customers": [{"id": "9"}]})
        return httpx.Response(200, json={"orders": [
            {"order_id": "26116", "id": "26116", "customer_id": "9"},
            {"order_id": "26117", "id": "26117", "customer_id": "9"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        result = await execute(tools(f"Consulta pelo CPF {CPF}", adapter), document=CPF)
    assert result["error"] == "order_selection_required"
    encoded = json.dumps(result)
    assert "26116" not in encoded and "26117" not in encoded


@pytest.mark.asyncio
async def test_previously_confirmed_reference_can_be_reused():
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"order": ORDER})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        result = await execute(
            tools("E o prazo?", adapter, continuity={"order_lookup": {"order_reference": "26116"}}),
            order_reference="26116")
    assert seen == ["/internal/orders/26116/complete"]
    assert result["estimated_delivery_date"] == "2026-10-10"


@pytest.mark.asyncio
async def test_upstream_failure_is_normalized():
    def respond(request):
        return httpx.Response(503, json={"error": "segredo do adaptador"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=12) as http:
        adapter = TrayAdapterClient("https://adapter.test", "private-token", http)
        adapter.max_get_attempts = 1
        result = await execute(tools("Status do pedido 26116", adapter), order_reference="26116")
    assert result["ok"] is False and result["error"] == "order_lookup_unavailable"
    assert "segredo" not in json.dumps(result)
