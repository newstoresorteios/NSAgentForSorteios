import httpx
import pytest

from app.tray_adapter_client import TrayAdapterClient, TrayAdapterError


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"products": []}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_product_search_sends_bearer_params_and_limit():
    fake = FakeClient(FakeResponse(payload={"products": []}))
    client = TrayAdapterClient("https://tray.example/", "secret", fake)
    await client.search_products(name="Tissot", ean=None, brand=None, limit=50, page=None)
    args, kwargs = fake.calls[0]
    assert args == ("GET", "https://tray.example/internal/products")
    assert kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert kwargs["params"] == {"name": "Tissot", "limit": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
async def test_http_errors_are_typed(status):
    fake = FakeClient(FakeResponse(status_code=status))
    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient("https://tray.example", "secret", fake).get_product(1)
    assert error.value.status_code == status


@pytest.mark.asyncio
async def test_connection_failure_is_safe():
    class BrokenClient:
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient("https://tray.example", "secret", BrokenClient()).get_product(1)
    assert error.value.status_code is None
