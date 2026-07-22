from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings


class TrayAdapterError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TrayAdapterClient:
    timeout_seconds = 75.0

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 http_client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.tray_adapter_url).rstrip("/")
        self.token = token if token is not None else settings.tray_adapter_token
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.base_url or not self.token:
            raise TrayAdapterError("tray_adapter_not_configured")
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.request(method, f"{self.base_url}{path}", headers=self._headers(), params=clean_params)
            if response.status_code >= 400:
                raise TrayAdapterError(f"tray_adapter_http_{response.status_code}", response.status_code)
            return response.json()
        except TrayAdapterError as exc:
            print("[tray.client] request_failed", {
                "error_type": type(exc).__name__,
                "status_code": exc.status_code,
                "timeout": isinstance(exc.__cause__, httpx.TimeoutException),
            })
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            print("[tray.client] request_failed", {
                "error_type": type(exc).__name__,
                "status_code": None,
                "timeout": isinstance(exc, httpx.TimeoutException),
            })
            raise TrayAdapterError("tray_adapter_unavailable") from exc
        except ValueError as exc:
            print("[tray.client] request_failed", {
                "error_type": type(exc).__name__,
                "status_code": None,
                "timeout": False,
            })
            raise TrayAdapterError("tray_adapter_invalid_response") from exc
        finally:
            if own_client:
                await client.aclose()

    async def search_products(self, *, name: str | None = None, reference: str | None = None,
                              ean: str | None = None, brand: str | None = None,
                              category_id: str | int | None = None, available: Any = None,
                              available_in_store: Any = None, stock: Any = None,
                              promotion: Any = None, limit: int = 5,
                              page: int | None = None) -> Any:
        return await self._request("GET", "/internal/products", params={
            "name": name, "reference": reference, "ean": ean, "brand": brand,
            "category_id": category_id, "available": available,
            "available_in_store": available_in_store, "stock": stock,
            "promotion": promotion, "limit": min(max(limit, 1), 20), "page": page,
        })

    async def get_product(self, product_id: str | int) -> Any:
        return await self._request("GET", f"/internal/products/{product_id}")

    async def get_product_stock(self, product_id: str | int) -> Any:
        return await self._request("GET", f"/internal/products/{product_id}/stock")

    async def list_product_variants(self, product_id: str | int) -> Any:
        return await self._request(
            "GET",
            "/internal/products/variants",
            params={"product_id": product_id},
        )

    async def get_product_variant(self, variant_id: str | int) -> Any:
        return await self._request("GET", f"/internal/products/variants/{variant_id}")

    async def list_categories(self, *, limit: int = 100, page: int = 1) -> Any:
        return await self._request(
            "GET",
            "/internal/categories",
            params={"limit": min(max(limit, 1), 100), "page": max(page, 1)},
        )

    async def get_category(self, category_id: str | int) -> Any:
        return await self._request("GET", f"/internal/categories/{category_id}")

    async def get_category_tree(self, category_id: str | int) -> Any:
        return await self._request("GET", f"/internal/categories/tree/{category_id}")

    async def list_brands(self, **params: Any) -> Any:
        return await self._request("GET", "/internal/brands", params=params)

    async def get_brand(self, brand_id: str | int) -> Any:
        return await self._request("GET", f"/internal/brands/{brand_id}")

    async def list_customers(self, **params: Any) -> Any:
        params.setdefault("limit", 5)
        return await self._request("GET", "/internal/customers", params=params)

    async def get_customer(self, customer_id: str | int) -> Any:
        return await self._request("GET", f"/internal/customers/{customer_id}")

    async def list_coupons(self, **params: Any) -> Any:
        params.setdefault("limit", 5)
        return await self._request("GET", "/internal/coupons", params=params)

    async def get_coupon(self, coupon_id: str | int) -> Any:
        return await self._request("GET", f"/internal/coupons/{coupon_id}")
