from __future__ import annotations

import json
import re
from typing import Any

from openai import APIError, AsyncOpenAI

from .commerce_router import (
    extract_product_query,
    handle_commerce_message,
    resolve_commerce_action,
)
from .config import get_settings
from .models import AgentResult, IncomingMessage


SALES_PLANNER_INSTRUCTIONS = """
Você planeja consultas comerciais para a New Store. Retorne somente JSON válido.
Campos permitidos: intent, query, filters, budget_max.
intent deve ser product_search, price, inventory, coupon ou recommendation.
Não retorne preço, estoque, disponibilidade, promoção ou condições de pagamento.
Extraia apenas a intenção, o termo e filtros informados pelo cliente.
""".strip()

SALES_RESPONDER_INSTRUCTIONS = """
Você é um vendedor objetivo e prestativo da New Store.
Use exclusivamente os fatos comerciais retornados pelo TrayAdapter no bloco FACTS.
Não invente produto, preço, estoque, promoção, disponibilidade, Pix, parcelamento ou cupom.
Se um fato não estiver em FACTS, diga que não foi informado.
Responda em português do Brasil, de forma curta para WhatsApp.
""".strip()

_ACTION_TO_PLAN = {
    "product_search": "product_search",
    "product_price": "price",
    "product_inventory": "inventory",
    "coupon_search": "coupon",
}


def _brand_from_query(query: str) -> str | None:
    first = query.split(maxsplit=1)[0] if query else ""
    return first or None


def deterministic_sales_plan(text: str | None) -> dict[str, Any] | None:
    action = resolve_commerce_action(text)
    if not action:
        return None
    query = extract_product_query(text)
    plan: dict[str, Any] = {
        "intent": _ACTION_TO_PLAN.get(action, "product_search"),
        "query": query,
        "filters": {},
    }
    if query and plan["intent"] in {"product_search", "price", "inventory", "recommendation"}:
        if len(query.split()) > 1 and not re.fullmatch(r"[A-Za-z0-9._/-]+", query):
            plan["filters"]["brand"] = _brand_from_query(query)
    return plan


def _parse_plan(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    allowed_intents = {"product_search", "price", "inventory", "coupon", "recommendation"}
    intent = parsed.get("intent")
    if intent not in allowed_intents:
        return None
    query = parsed.get("query")
    if query is not None and not isinstance(query, str):
        return None
    filters = parsed.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    return {
        "intent": intent,
        "query": (query or "").strip(),
        "filters": {key: value for key, value in filters.items() if key in {"brand", "category_id", "budget_max", "style", "color"}},
        "budget_max": parsed.get("budget_max"),
    }


async def plan_sales_request(message: IncomingMessage) -> dict[str, Any] | None:
    fallback = deterministic_sales_plan(message.text)
    settings = get_settings()
    if not fallback or not settings.openai_api_key:
        return fallback

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SALES_PLANNER_INSTRUCTIONS},
                {"role": "user", "content": message.text or ""},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = _parse_plan(response.choices[0].message.content if response.choices else None)
        return parsed or fallback
    except (APIError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print("[sales.planner] failed", {"error_type": type(exc).__name__})
        return fallback


def _planned_message(message: IncomingMessage, plan: dict[str, Any]) -> IncomingMessage:
    query = str(plan.get("query") or "").strip()
    intent = plan.get("intent")
    if intent == "inventory":
        routed_text = f"Tem estoque de {query}?" if query else "Tem estoque?"
    elif intent == "price":
        routed_text = f"Quanto custa {query}?" if query else "Quanto custa?"
    elif intent == "coupon":
        routed_text = "Tem algum cupom comercial disponível?"
    else:
        routed_text = f"Tem {query}?" if query else message.text
    return message.model_copy(update={"text": routed_text})


async def _sales_response_with_openai(
    message: IncomingMessage,
    plan: dict[str, Any],
    tray_result: AgentResult,
) -> AgentResult | None:
    settings = get_settings()
    if not settings.openai_api_key or tray_result.safety_reason in {
        "tray_adapter_unavailable", "product_not_found", "ambiguous_product", "product_context_missing", "coupon_not_found"
    }:
        return None
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SALES_RESPONDER_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"original_message": message.text, "plan": plan, "FACTS": tray_result.reply_text},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            return None
        return AgentResult(reply_text=content.strip(), intent="commerce", handoff_required=False)
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.responder] failed", {"error_type": type(exc).__name__})
        return None


async def handle_sales_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
) -> AgentResult | None:
    plan = await plan_sales_request(message)
    if not plan:
        return None
    print("[sales.plan]", {"intent": plan.get("intent"), "has_query": bool(plan.get("query"))})
    routed_message = _planned_message(message, plan)
    tray_result = await handle_commerce_message(routed_message, facts, customer_context)
    if tray_result is None:
        return None
    final = await _sales_response_with_openai(message, plan, tray_result)
    return final or tray_result
