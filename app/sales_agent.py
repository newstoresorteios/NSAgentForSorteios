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
from .guardrails import (
    detect_commerce_inquiry,
    detect_current_raffle_inquiry,
    detect_raffle_history_inquiry,
    detect_rules_inquiry,
    detect_balance_inquiry,
    detect_coupon_code_inquiry,
)
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

OUT_OF_SCOPE_REPLY = "Posso ajudar com produtos, compras, pedidos e informações da NewStore, além dos sorteios da loja."
GREETING_REPLY = "Olá! Como posso ajudar?"
SCOPE_INSTRUCTIONS = """
Classifique a mensagem para um vendedor da NewStore. Retorne somente JSON válido.
domain deve ser commerce, raffle, greeting ou out_of_scope.
Para commerce, action deve ser purchase_intent, product_search, recommendation,
product_price, product_inventory, product_comparison, coupon_search ou clarification.
Extraia product_type, product_query, brand, model, reference, ean, budget_min,
budget_max e attributes quando existirem. Nunca produza fatos comerciais.
Assuntos externos à NewStore e aos sorteios são out_of_scope.
""".strip()

_ACTION_TO_PLAN = {
    "product_search": "product_search",
    "product_price": "price",
    "product_inventory": "inventory",
    "coupon_search": "coupon",
}


def _is_greeting(text: str | None) -> bool:
    normalized = " ".join((text or "").lower().strip().split()).strip("!?.,")
    return normalized in {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "oi tudo bem", "olá tudo bem", "ola tudo bem"}


def deterministic_scope(text: str | None) -> dict[str, Any]:
    value = (text or "").strip()
    normalized = value.lower()
    if _is_greeting(value):
        return {"domain": "greeting", "action": "greeting", "_source": "fallback"}
    if detect_balance_inquiry(value) or detect_coupon_code_inquiry(value) or detect_raffle_history_inquiry(value) or detect_current_raffle_inquiry(value) or detect_rules_inquiry(value) or "sorteio" in normalized:
        return {"domain": "raffle", "action": "local_flow", "_source": "fallback"}
    if detect_commerce_inquiry(value) or normalized.startswith(("tem ", "vocês têm ", "voces tem ", "vende ")) or any(term in normalized for term in ("comprar", "adquirir", "quero ", "procuro", "busco", "orçamento", "orcamento", "comparar", "recomende")):
        plan = deterministic_sales_plan(value) or {}
        return {"domain": "commerce", **plan, "_source": "fallback"}
    store_terms = ("newstore", "new store", "loja", "pedido", "compra", "atendimento comercial", "catálogo", "catalogo")
    if any(term in normalized for term in store_terms):
        return {"domain": "store_general", "action": "store_general", "_source": "fallback"}
    return {"domain": "out_of_scope", "action": "scope_refusal", "_source": "fallback"}


def _normalize_semantic_plan(parsed: dict[str, Any]) -> dict[str, Any] | None:
    domain = parsed.get("domain")
    if domain not in {"commerce", "raffle", "greeting", "store_general", "out_of_scope"}:
        return None
    normalized: dict[str, Any] = {"domain": domain, "action": parsed.get("action"), "_source": "openai"}
    if domain != "commerce":
        return normalized
    action = parsed.get("action")
    allowed = {"purchase_intent", "product_search", "recommendation", "product_price", "product_inventory", "product_comparison", "coupon_search", "clarification"}
    if action not in allowed:
        return None
    query = parsed.get("product_query") or parsed.get("product_type") or parsed.get("model") or parsed.get("reference") or parsed.get("ean") or ""
    filters: dict[str, Any] = {}
    for key in ("brand", "model", "reference", "ean", "budget_min", "budget_max", "attributes"):
        if parsed.get(key) is not None:
            filters[key] = parsed[key]
    attributes = parsed.get("attributes")
    if isinstance(attributes, list) and attributes:
        query = " ".join([str(query), *[str(item) for item in attributes]]).strip()
    normalized.update({
        "intent": action,
        "query": str(query).strip(),
        "filters": filters,
        "budget_max": parsed.get("budget_max"),
        "product_type": parsed.get("product_type"),
    })
    return normalized


def _parse_scope(content: str | None) -> dict[str, Any] | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _normalize_semantic_plan(parsed) if isinstance(parsed, dict) else None


async def interpret_message(message: IncomingMessage) -> dict[str, Any]:
    fallback = deterministic_scope(message.text)
    if fallback.get("domain") == "greeting":
        return fallback
    settings = get_settings()
    if not settings.openai_api_key:
        return fallback
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SCOPE_INSTRUCTIONS},
                {"role": "user", "content": message.text or ""},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return _parse_scope(response.choices[0].message.content if response.choices else None) or fallback
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.scope] failed", {"error_type": type(exc).__name__})
        return fallback


def _brand_from_query(query: str) -> str | None:
    first = query.split(maxsplit=1)[0] if query else ""
    return first or None


def deterministic_sales_plan(text: str | None) -> dict[str, Any] | None:
    normalized = (text or "").lower()
    purchase = any(term in normalized for term in ("quero comprar", "quero adquirir", "quero um ", "quero uma ", "gostaria de comprar", "gostaria de um ", "procuro", "busco", "recomende"))
    action = resolve_commerce_action(text)
    if purchase and not any(term in normalized for term in ("quanto custa", "preço", "preco", "estoque", "disponibilidade")):
        action = "purchase_intent"
    if not action:
        return None
    query = extract_product_query(text)
    if query.lower().strip() in {"alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa"}:
        query = ""
    plan: dict[str, Any] = {
        "intent": "purchase_intent" if action == "purchase_intent" else _ACTION_TO_PLAN.get(action, "product_search"),
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
    elif intent in {"product_search", "purchase_intent", "recommendation", "product_comparison"}:
        routed_text = f"Tem {query}?" if query else message.text
    else:
        routed_text = message.text
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
    semantic_plan: dict[str, Any] | None = None,
) -> AgentResult | None:
    plan = semantic_plan if semantic_plan and semantic_plan.get("domain") == "commerce" else await plan_sales_request(message)
    if not plan:
        return None
    print("[sales.agent] planner", {
        "source": plan.get("_source", "fallback"),
        "action": plan.get("intent"),
        "has_query": bool(plan.get("query")),
        "has_brand": bool((plan.get("filters") or {}).get("brand")),
        "has_model": bool((plan.get("filters") or {}).get("model")),
    })
    vague_query = str(plan.get("query") or "").strip().lower() in {"", "alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa", "produto"}
    if plan.get("intent") == "clarification" and vague_query:
        return AgentResult(reply_text="Claro. Está procurando relógio, acessório ou outro tipo de produto?", intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    if plan.get("intent") in {"purchase_intent", "recommendation"} and vague_query:
        return AgentResult(reply_text="Claro. Está procurando relógio, acessório ou outro tipo de produto?", intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    if plan.get("intent") == "product_search" and vague_query:
        return AgentResult(reply_text="Qual produto você quer encontrar? Informe o nome, modelo ou referência.", intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    routed_message = _planned_message(message, plan)
    queries = [str(plan.get("query") or "").strip()]
    code_value = re.sub(r"^(?:ean|sku|ref(?:er[êe]ncia)?)\s+", "", queries[0], flags=re.IGNORECASE)
    code_query = bool(re.fullmatch(r"[A-Za-z0-9._/-]+", code_value)) and any(char.isdigit() for char in code_value)
    if plan.get("intent") == "product_search" and len(queries[0].split()) > 1 and not code_query:
        queries.append(queries[0].split()[-1])
        brand = (plan.get("filters") or {}).get("brand")
        if brand:
            queries.append(str(brand))
    tray_result = None
    for attempt, query in enumerate(queries[:3], start=1):
        attempt_plan = {**plan, "query": query}
        print("[sales.agent] tray_request", {"tool": "search_products", "attempt": attempt, "strategy": "initial" if attempt == 1 else "progressive"})
        tray_result = await handle_commerce_message(_planned_message(message, attempt_plan), facts, customer_context)
        print("[sales.agent] tray_result", {"ok": tray_result is not None and tray_result.safety_reason != "tray_adapter_unavailable", "results_count": 0 if tray_result and tray_result.safety_reason == "product_not_found" else None})
        if not tray_result or tray_result.safety_reason != "product_not_found":
            break
    if tray_result is None:
        return None
    if plan.get("intent") in {"purchase_intent", "recommendation", "clarification"} and tray_result.safety_reason == "product_not_found":
        return AgentResult(reply_text="Não encontrei opções compatíveis no catálogo agora. Posso tentar outro tipo ou faixa de produto?", intent="commerce", handoff_required=False, safety_reason="recommendation_not_found")
    final = await _sales_response_with_openai(message, plan, tray_result)
    print("[sales.agent] responder", {"source": "openai" if final else "deterministic_fallback"})
    return final or tray_result
