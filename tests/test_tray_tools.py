import pytest

from app.tray_tools import execute_tool, search_products


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


@pytest.mark.asyncio
async def test_search_products_reduces_payload_and_uses_name():
    client = FakeTray()
    result = await execute_tool("search_products", {"query": "Tissot Seastar", "limit": 5}, client)
    assert result == {"products": [{"id": "641", "name": "Tissot Seastar", "price": 6399.99}]}
    assert client.calls[0][1]["name"] == "Tissot Seastar"


@pytest.mark.asyncio
async def test_product_and_inventory_tools_call_expected_methods():
    client = FakeTray()
    assert (await execute_tool("get_product", {"product_id": "641"}, client))["current_price"] == 10
    inventory = await execute_tool("check_inventory", {"product_id": "641"}, client)
    assert inventory["stock"] == 0
    assert inventory["upon_request"] is True
    assert [call[0] for call in client.calls] == ["get_product", "get_product_stock"]
