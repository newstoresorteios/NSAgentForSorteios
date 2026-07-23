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
    assert kwargs["params"] == {"name": "Tissot", "limit": 20}


@pytest.mark.asyncio
async def test_categories_and_variants_use_new_read_only_routes():
    fake = FakeClient()
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.list_categories(limit=500, page=2)
    await client.get_category("10")
    await client.get_category_tree("10")
    await client.list_product_variants("641")
    await client.get_product_variant("V1")

    assert [call[0][1] for call in fake.calls] == [
        "https://tray.example/internal/categories",
        "https://tray.example/internal/categories/10",
        "https://tray.example/internal/categories/tree/10",
        "https://tray.example/internal/products/variants",
        "https://tray.example/internal/products/variants/V1",
    ]
    assert fake.calls[0][1]["params"] == {"limit": 50, "page": 2}
    assert fake.calls[3][1]["params"] == {"product_id": "641"}


@pytest.mark.asyncio
async def test_cart_uses_exact_adapter_contract_and_omits_optional_fields():
    fake = FakeClient(FakeResponse(payload={
        "cart_id": "C1",
        "session_id": "S1",
        "cart_url": "https://loja.example/checkout/S1",
    }))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    result = await client.create_cart(
        product_id="641",
        quantity=2,
        price="5439.99",
    )

    args, kwargs = fake.calls[0]
    assert args == ("POST", "https://tray.example/internal/carts")
    assert kwargs["json"] == {
        "product_id": "641",
        "quantity": 2,
        "price": "5439.99",
    }
    assert result["session_id"] == "S1"


@pytest.mark.asyncio
async def test_cart_post_is_never_retried_on_transient_error():
    fake = FakeClient(FakeResponse(status_code=503))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    with pytest.raises(TrayAdapterError):
        await client.create_cart(
            product_id="641",
            variant_id="V1",
            quantity=1,
            price="10.00",
            session_id="S1",
        )

    assert len(fake.calls) == 1
    assert fake.calls[0][1]["json"] == {
        "product_id": "641",
        "variant_id": "V1",
        "quantity": 1,
        "price": "10.00",
        "session_id": "S1",
    }


@pytest.mark.asyncio
async def test_get_cart_uses_adapter_session_route():
    fake = FakeClient(FakeResponse(payload={"session_id": "S1"}))
    client = TrayAdapterClient("https://tray.example", "secret", fake)

    await client.get_cart("S1")

    assert fake.calls[0][0] == (
        "GET",
        "https://tray.example/internal/carts/S1",
    )


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


@pytest.mark.asyncio
async def test_transient_503_is_retried_once_and_can_recover(monkeypatch):
    import app.tray_adapter_client as tray_client

    class SequenceClient:
        def __init__(self):
            self.responses = [
                FakeResponse(status_code=503),
                FakeResponse(status_code=200, payload={"products": [{"id": "ok"}]}),
            ]
            self.calls = []

        async def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.responses.pop(0)

    async def no_wait(_seconds):
        return None

    fake = SequenceClient()
    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)

    result = await TrayAdapterClient(
        "https://tray.example",
        "secret",
        fake,
    ).search_products(brand="Doxa", limit=20, page=1)

    assert result["products"] == [{"id": "ok"}]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_persistent_503_fails_after_single_retry(monkeypatch):
    import app.tray_adapter_client as tray_client

    class AlwaysUnavailableClient:
        def __init__(self):
            self.calls = []

        async def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeResponse(status_code=503)

    async def no_wait(_seconds):
        return None

    fake = AlwaysUnavailableClient()
    monkeypatch.setattr(tray_client.asyncio, "sleep", no_wait)

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).search_products(brand="Doxa", limit=20, page=1)

    assert error.value.status_code == 503
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_non_transient_422_is_not_retried():
    fake = FakeClient(FakeResponse(status_code=422))

    with pytest.raises(TrayAdapterError) as error:
        await TrayAdapterClient(
            "https://tray.example",
            "secret",
            fake,
        ).search_products(brand="Doxa", limit=20, page=1)

    assert error.value.status_code == 422
    assert len(fake.calls) == 1
