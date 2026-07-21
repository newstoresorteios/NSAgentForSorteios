from __future__ import annotations

import re
from typing import Any

from .tray_adapter_client import TrayAdapterClient, TrayAdapterError


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais na loja.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e regras de disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico, quando necessário.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_coupons", "description": "Consultar cupons quando a conversa precisar disso.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_coupon", "description": "Consultar detalhes de um cupom.", "parameters": {"type": "object", "properties": {"coupon_id": {"type": "string"}}, "required": ["coupon_id"], "additionalProperties": False}}},
]

_PRODUCT_FIELDS = ("id", "name", "reference", "ean", "brand", "model", "price", "promotional_price", "current_price", "stock", "available", "availability", "available_in_store", "available_for_purchase", "upon_request", "when_stock_runs_out", "payment_option", "payment_option_details", "url")
_CUSTOMER_FIELDS = ("id", "name", "email", "city", "state", "last_purchase", "total_orders")
_COUPON_FIELDS = ("id", "code", "description", "starts_at", "ends_at", "value", "type", "value_start", "value_end", "usage_counter_limit", "usage_counter_limit_customer", "coupon_type", "local_application", "freight_application")


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "products", "customers", "coupons", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return [payload] if isinstance(payload, dict) else []


def _reduce(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"value": item}
    return {key: item[key] for key in fields if key in item and item[key] is not None}


def _reduce_products(payload: Any) -> dict[str, Any]:
    return {"products": [_reduce(item, _PRODUCT_FIELDS) for item in _items(payload)[:5]]}


def _query_filters(query: str) -> list[dict[str, str]]:
    value = query.strip()
    if re.fullmatch(r"\d{8,14}", value):
        return [{"ean": value}, {"reference": value}]
    if re.search(r"[./_-]", value) or (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
        return [{"reference": value}, {"name": value}]
    return [{"name": value}, {"brand": value}]


async def search_products(client: TrayAdapterClient, **args: Any) -> dict[str, Any]:
    query = (args.pop("query", None) or "").strip()
    explicit = {key: args.get(key) for key in ("name", "reference", "ean", "brand") if args.get(key) is not None}
    attempts = [explicit or filters for filters in _query_filters(query)] if query else [explicit]
    for filters in attempts:
        if not filters:
            continue
        payload = await client.search_products(**filters, limit=args.get("limit", 5))
        result = _reduce_products(payload)
        if result["products"] or len(attempts) == 1:
            return result
    return {"products": []}


async def execute_tool(name: str, arguments: dict[str, Any], client: TrayAdapterClient | None = None) -> dict[str, Any]:
    client = client or TrayAdapterClient()
    try:
        if name == "search_products":
            return await search_products(client, **arguments)
        if name == "get_product":
            return _reduce((await client.get_product(arguments["product_id"])), _PRODUCT_FIELDS)
        if name == "check_inventory":
            return _reduce(await client.get_product_stock(arguments["product_id"]), ("product_id", "stock", "available", "available_in_store", "available_for_purchase", "upon_request", "availability", "when_stock_runs_out"))
        if name == "search_customer":
            return {"customers": [_reduce(item, _CUSTOMER_FIELDS) for item in _items(await client.list_customers(**arguments))[:5]]}
        if name == "get_customer":
            return _reduce(await client.get_customer(arguments["customer_id"]), _CUSTOMER_FIELDS)
        if name == "list_coupons":
            return {"coupons": [_reduce(item, _COUPON_FIELDS) for item in _items(await client.list_coupons(**arguments))[:5]]}
        if name == "get_coupon":
            return _reduce(await client.get_coupon(arguments["coupon_id"]), _COUPON_FIELDS)
        raise ValueError(f"unknown_tool:{name}")
    except TrayAdapterError as exc:
        print("[tray.tool] request_failed", {"tool": name, "status_code": exc.status_code})
        return {"error": "Não consegui consultar o sistema da loja neste momento."}
