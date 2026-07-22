from __future__ import annotations

import re
import time
import html
from decimal import Decimal, InvalidOperation
from typing import Any

from .tray_adapter_client import TrayAdapterClient, TrayAdapterError


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais na loja.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "available": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e regras de disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico, quando necessário.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_coupons", "description": "Consultar cupons quando a conversa precisar disso.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_coupon", "description": "Consultar detalhes de um cupom.", "parameters": {"type": "object", "properties": {"coupon_id": {"type": "string"}}, "required": ["coupon_id"], "additionalProperties": False}}},
]

TOOL_REGISTRY = {
    "commerce": ("search_products", "get_product", "check_inventory", "search_customer", "get_customer", "list_coupons", "get_coupon"),
    "raffle": ("rules", "balance", "coupon_code", "raffle_history", "current_raffle", "simulation"),
}

_PRODUCT_FIELDS = ("id", "name", "reference", "ean", "brand", "model", "description", "category", "category_id", "attributes", "properties", "color", "style", "material", "price", "promotional_price", "current_price", "stock", "available", "availability", "available_in_store", "available_for_purchase", "upon_request", "when_stock_runs_out", "payment_option", "payment_option_details", "url")
_CUSTOMER_FIELDS = ("id", "name", "email", "city", "state", "last_purchase", "total_orders")
_COUPON_FIELDS = ("id", "code", "description", "starts_at", "ends_at", "value", "type", "value_start", "value_end", "usage_counter_limit", "usage_counter_limit_customer", "coupon_type", "local_application", "freight_application")


def _clean_text(value: str) -> str:
    return html.unescape(value).strip()


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    return value


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value).replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_payment_options(value: Any) -> Any:
    if not isinstance(value, list):
        return _clean_value(value)
    normalized: dict[str, Any] = {"pix": None, "installments": []}
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("display_name") or "").lower()
        amount = _number(item.get("value"))
        if "pix" in kind:
            normalized["pix"] = {"value": amount} if amount is not None else {}
            continue
        count = item.get("plots") or item.get("installments") or item.get("count")
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        normalized["installments"].append({
            "count": count,
            "value": amount,
            "interest": bool(_number(item.get("tax")) or 0),
        })
    if normalized["pix"] is None:
        normalized.pop("pix")
    if not normalized["installments"]:
        normalized.pop("installments")
    return normalized


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
    result: dict[str, Any] = {}
    for key in fields:
        if key not in item or item[key] is None:
            continue
        result[key] = _normalize_payment_options(item[key]) if key in {"payment_option", "payment_option_details"} else _clean_value(item[key])
    return result


def _reduce_products(payload: Any, limit: int = 5) -> dict[str, Any]:
    safe_limit = min(max(int(limit), 1), 20)
    return {"products": [_reduce(item, _PRODUCT_FIELDS) for item in _items(payload)[:safe_limit]]}


def _query_filters(query: str) -> list[dict[str, str]]:
    value = query.strip()
    ean_match = re.fullmatch(r"ean\s+(\d{8,14})", value, flags=re.IGNORECASE)
    if ean_match:
        return [{"ean": ean_match.group(1)}]
    if re.fullmatch(r"\d{8,14}", value):
        return [{"ean": value}, {"reference": value}]
    if re.search(r"[./_-]", value) or (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
        return [{"reference": value}, {"name": value}]
    return [{"name": value}]


async def search_products(client: TrayAdapterClient, **args: Any) -> dict[str, Any]:
    query = (args.pop("query", None) or "").strip()
    supported = ("name", "reference", "ean", "brand", "category_id", "available", "stock", "promotion", "page")
    explicit = {key: args.get(key) for key in supported if args.get(key) is not None}
    attempts = [explicit or filters for filters in _query_filters(query)] if query else [explicit]
    limit = min(max(int(args.get("limit", 5)), 1), 20)
    for filters in attempts:
        if not filters:
            continue
        payload = await client.search_products(**filters, limit=limit)
        result = _reduce_products(payload, limit)
        if result["products"] or len(attempts) == 1:
            return result
    return {"products": []}


async def _execute_tool(name: str, arguments: dict[str, Any], client: TrayAdapterClient | None = None) -> dict[str, Any]:
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
async def execute_tool(name: str, arguments: dict[str, Any], client: TrayAdapterClient | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await _execute_tool(name, arguments, client)
        ok = "error" not in result
        print("[tray.tool] executed", {"tool": name, "ok": ok, "elapsed_ms": round((time.perf_counter() - started) * 1000)})
        print("[sales.tool]", {"tool": name, "success": ok})
        if not ok:
            return {"error": "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."}
        return result
    except Exception as exc:
        print("[tray.tool] executed", {"tool": name, "ok": False, "elapsed_ms": round((time.perf_counter() - started) * 1000), "error_type": type(exc).__name__})
        print("[sales.tool]", {"tool": name, "success": False})
        return {"error": "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."}
