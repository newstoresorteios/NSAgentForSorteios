import pytest

from app.tray_tools import TOOL_SCHEMAS, _reduce, execute_tool, search_products
from app.tray_adapter_client import TrayAdapterError


class FakeTray:
    def __init__(self):
        self.calls = []

    async def search_products(self, **kwargs):
        self.calls.append(("search_products", kwargs))
        return {"products": [{"id": "641", "name": "Tissot Seastar", "price": 6399.99, "huge": "omit"}]}

    async def get_product(self, product_id):
        self.calls.append(("get_product", product_id))
        return {"id": product_id, "name": "Produto", "current_price": 10, "secret_blob": "omit"}

    async def get_product_stock(self, product_id):
        self.calls.append(("get_product_stock", product_id))
        return {"product_id": product_id, "stock": 0, "available": "0", "upon_request": True, "availability": "sob consulta"}

    async def list_categories(self, **kwargs):
        self.calls.append(("list_categories", kwargs))
        return {
            "categories": [{"id": 10, "name": "Relógios", "secret": "omit"}],
            "paging": {"total": 1, "page": 1, "limit": 50},
        }

    async def get_category_tree(self, category_id):
        self.calls.append(("get_category_tree", category_id))
        return {"id": category_id, "name": "Relógios", "children": [{"id": 11, "name": "Masculinos"}]}

    async def list_product_variants(self, product_id):
        self.calls.append(("list_product_variants", product_id))
        return {"variants": [{"id": "V1", "product_id": product_id, "color": "Preto", "stock": 2, "secret": "omit"}]}

    async def create_cart(self, **kwargs):
        self.calls.append(("create_cart", kwargs))
        return {
            "cart_id": "C1",
            "session_id": "S1",
            "cart_url": "https://loja.example/checkout/S1",
            "secret": "omit",
        }

    async def get_cart(self, session_id):
        self.calls.append(("get_cart", session_id))
        return {
            "cart_id": "C1",
            "session_id": session_id,
            "cart_url": "https://loja.example/checkout/S1",
            "secret": "omit",
        }

    async def get_cart_complete(self, session_id):
        self.calls.append(("get_cart_complete", session_id))
        return {
            "data": {
                "cart": {
                    "cart_id": "C1",
                    "session_id": session_id,
                    "total": "199.80",
                    "items": [{
                        "product_id": "641",
                        "variant_id": "V1",
                        "quantity": 2,
                        "unit_price": "99.90",
                    }],
                }
            }
        }

    async def get_payment_options(self, cart_session_id):
        self.calls.append(("get_payment_options", cart_session_id))
        return {
            "payment_options": [
                {
                    "id": "PIX",
                    "name": "Pix",
                    "text": "Pagamento via Pix",
                    "card": 0,
                    "discount_value": "9.99",
                    "total_base": "189.81",
                    "plots": [{
                        "installments": 1,
                        "value": "189.81",
                        "interest": 0,
                        "order_total": "189.81",
                    }],
                },
                {
                    "id": "CARD",
                    "name": "Cartão",
                    "text": "Cartão de crédito",
                    "card": 1,
                    "plots": [{
                        "installments": 10,
                        "value": "19.98",
                        "interest": 0,
                        "order_total": "199.80",
                    }],
                },
            ]
        }


@pytest.mark.asyncio
async def test_search_products_reduces_payload_and_uses_name():
    client = FakeTray()
    result = await execute_tool("search_products", {"query": "Tissot Seastar", "limit": 5}, client)
    assert result == {"products": [{"id": "641", "name": "Tissot Seastar", "price": 6399.99}]}
    assert client.calls[0][1]["name"] == "Tissot Seastar"


@pytest.mark.asyncio
async def test_search_products_preserves_compact_paging_metadata():
    class PagedTray(FakeTray):
        async def search_products(self, **kwargs):
            self.calls.append(("search_products", kwargs))
            return {
                "products": [{"id": "1", "name": "Produto"}],
                "paging": {"total": 41, "page": 2, "limit": 20},
            }

    result = await execute_tool(
        "search_products",
        {"brand": "Marca", "limit": 20, "page": 2},
        PagedTray(),
    )

    assert result["paging"] == {"total": 41, "page": 2, "limit": 20}


@pytest.mark.asyncio
async def test_product_and_inventory_tools_call_expected_methods():
    client = FakeTray()
    assert (await execute_tool("get_product", {"product_id": "641"}, client))["current_price"] == 10
    inventory = await execute_tool("check_inventory", {"product_id": "641"}, client)
    assert inventory["stock"] == 0
    assert inventory["upon_request"] is True
    assert [call[0] for call in client.calls] == ["get_product", "get_product_stock"]


def test_tray_text_and_payment_options_are_normalized():
    result = _reduce(
        {
            "name": "Relógio &agrave; vista",
            "payment_option_details": [
                {
                    "id": "PIX",
                    "name": "Pix - Vindi",
                    "text": "Pagamento instantâneo",
                    "card": 0,
                    "total_base": "5439.99",
                    "plots": [{
                        "installments": 1,
                        "value": "5439.99",
                        "interest": 0,
                    }],
                },
                {
                    "id": "CARD",
                    "name": "Cartão",
                    "text": "Crédito",
                    "card": 1,
                    "plots": [{
                        "installments": 12,
                        "value": "533.33",
                        "interest": 0,
                    }],
                },
            ],
        },
        ("name", "payment_option_details"),
    )
    assert result["name"] == "Relógio à vista"
    options = result["payment_option_details"]
    assert options["pix"]["name"] == "Pix - Vindi"
    assert options["card"]["name"] == "Cartão"
    assert options["installments"] == [{
        "count": 12,
        "value": 533.33,
        "interest": False,
    }]
    assert "display_name" not in str(result)


@pytest.mark.asyncio
async def test_category_and_variant_tools_reduce_payloads():
    client = FakeTray()

    categories = await execute_tool("list_categories", {"limit": 50, "page": 1}, client)
    tree = await execute_tool("get_category_tree", {"category_id": "10"}, client)
    variants = await execute_tool("list_product_variants", {"product_id": "641"}, client)

    assert categories == {
        "categories": [{"id": 10, "name": "Relógios"}],
        "paging": {"total": 1, "page": 1, "limit": 50},
    }
    assert tree["tree"]["children"] == [{"id": 11, "name": "Masculinos"}]
    assert variants == {"variants": [{
        "id": "V1",
        "product_id": "641",
        "color": "Preto",
        "stock": 2,
        "variant_id": "V1",
    }]}


@pytest.mark.asyncio
async def test_category_http_422_is_preserved_as_invalid_request():
    class InvalidCategoryTray:
        async def list_categories(self, **kwargs):
            raise TrayAdapterError("tray_adapter_http_422", status_code=422)

    result = await execute_tool(
        "list_categories",
        {"limit": 50, "page": 1},
        InvalidCategoryTray(),
    )

    assert result["error_reason"] == "category_invalid_request"


@pytest.mark.asyncio
async def test_cart_tools_use_normalized_adapter_contract():
    client = FakeTray()

    created = await execute_tool(
        "create_cart",
        {
            "product_id": "641",
            "variant_id": "V1",
            "quantity": 2,
            "price": "99.90",
        },
        client,
    )
    loaded = await execute_tool("get_cart", {"session_id": "S1"}, client)

    assert created == {
        "cart_id": "C1",
        "session_id": "S1",
        "cart_url": "https://loja.example/checkout/S1",
    }
    assert loaded["session_id"] == "S1"
    assert client.calls[-2] == (
        "create_cart",
        {
            "product_id": "641",
            "variant_id": "V1",
            "quantity": 2,
            "price": "99.90",
        },
    )


@pytest.mark.asyncio
async def test_wrapped_product_detail_preserves_commercial_price():
    class WrappedTray(FakeTray):
        async def get_product(self, product_id):
            return {
                "data": {
                    "product": {
                        "id": product_id,
                        "name": "Produto",
                        "current_price": "6199.99",
                        "available": True,
                    }
                }
            }

    product = await execute_tool(
        "get_product",
        {"product_id": "1025"},
        WrappedTray(),
    )

    assert product["id"] == "1025"
    assert product["current_price"] == "6199.99"


@pytest.mark.asyncio
async def test_complete_cart_and_payment_options_are_normalized():
    client = FakeTray()

    cart = await execute_tool(
        "get_cart_complete",
        {"session_id": "S1"},
        client,
    )
    payments = await execute_tool(
        "get_payment_options",
        {"cart_session_id": "S1"},
        client,
    )

    assert cart["total"] == "199.80"
    assert cart["items"] == [{
        "product_id": "641",
        "variant_id": "V1",
        "quantity": 2,
        "unit_price": "99.90",
    }]
    assert payments["payment_options"]["pix"]["total_base"] == 189.81
    assert payments["payment_options"]["installments"][0] == {
        "count": 10,
        "value": 19.98,
        "interest": False,
        "order_total": 199.8,
    }


def test_cart_side_effect_is_not_exposed_as_an_openai_tool():
    exposed = {
        schema["function"]["name"]
        for schema in TOOL_SCHEMAS
    }

    assert "create_cart" not in exposed
    assert "get_cart" not in exposed
