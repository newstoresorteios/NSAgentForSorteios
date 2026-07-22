from __future__ import annotations

import json
import re
import html
from typing import Any

from openai import APIError, AsyncOpenAI, BadRequestError
from pydantic import ValidationError

from .commerce_router import (
    extract_product_query,
    handle_commerce_message,
    resolve_commerce_action,
    _product_lines,
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
from .models import AgentResult, IncomingMessage, SalesInterpretation


SALES_PLANNER_INSTRUCTIONS = """
Você planeja consultas comerciais para a New Store. Retorne somente JSON válido.
Use este formato: domain, goal, subject, constraints, information_needed,
needs_clarification e clarification_question.
goal deve ser discover, find, recommend, compare, inspect, buy ou after_sales.
subject deve conter product_type, query, brand, model, reference e ean.
constraints deve conter budget_min, budget_max, attributes, color e style.
Não produza fatos comerciais nem diga que um produto existe.
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
SALES_INTERPRETER_INSTRUCTIONS = """
Você interpreta mensagens do atendimento da NewStore.

NÃO responda ao cliente. Analise a mensagem atual considerando o histórico
imediatamente anterior e extraia o estado comercial evidente. Mensagens curtas
frequentemente complementam uma conversa anterior. Nunca invente fatos comerciais.

Use domain=commerce para produtos, compras e continuações de uma descoberta de
produto; raffle para sorteios da NewStore; store_general para assuntos da loja sem
produto específico; greeting para saudação; out_of_scope somente quando a mensagem,
considerada junto ao histórico, não tiver relação com a NewStore.

Exemplo 1:
Histórico: cliente quer comprar um relógio; atendente pergunta se prefere esportivo,
social ou casual. Atual: esportivo.
Interpretação: domain=commerce, goal=discover, product_type=relógio,
style=esportivo, references_previous_context=true.

Exemplo 2:
Histórico: produto=relógio e style=esportivo. Atual: menos de 5 mil.
Interpretação: domain=commerce, goal=recommend, product_type=relógio,
style=esportivo, budget_max=5000, references_previous_context=true.

Exemplo 3:
Histórico: cliente pede recomendação de relógios; atendente pergunta o estilo.
Atual: social.
Interpretação: domain=commerce, product_type=relógio, style=social,
references_previous_context=true.

Exemplo 4:
Atual: preciso de um relógio para dar de presente, não queria gastar muito.
Interpretação: domain=commerce, goal=discover, product_type=relógio,
occasion=presente, needs_clarification=true. Como não há valor numérico, faça uma
única pergunta curta sobre a faixa aproximada em clarification_question.

Exemplo 5:
Atual: Tem Tissot Seastar?
Interpretação: domain=commerce, goal=find, brand=Tissot, model=Seastar.

Exemplo 6:
Atual sem contexto comercial: quem ganhou o jogo ontem?
Interpretação: domain=out_of_scope.

Não copie uma fala anterior como fato comercial. Preserve produto, preferências e
orçamento que estejam evidentes no contexto. confidence deve refletir a certeza da
interpretação entre 0 e 1.
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
    goal = parsed.get("goal")
    if not action and goal:
        action = {"find": "product_search", "recommend": "recommendation", "compare": "product_comparison", "inspect": "product_price", "buy": "purchase_intent", "discover": "clarification"}.get(goal)
    allowed = {"purchase_intent", "product_search", "recommendation", "product_price", "product_inventory", "product_comparison", "coupon_search", "clarification"}
    if action not in allowed:
        return None
    subject = parsed.get("subject") if isinstance(parsed.get("subject"), dict) else {}
    constraints_input = parsed.get("constraints") if isinstance(parsed.get("constraints"), dict) else {}
    query = subject.get("query") or parsed.get("product_query") or subject.get("product_type") or parsed.get("product_type") or subject.get("model") or parsed.get("model") or subject.get("reference") or parsed.get("reference") or subject.get("ean") or parsed.get("ean") or ""
    filters: dict[str, Any] = {}
    for key in ("brand", "model", "reference", "ean", "budget_min", "budget_max", "attributes"):
        value = subject.get(key) if key in {"brand", "model", "reference", "ean"} else constraints_input.get(key, parsed.get(key))
        if value is not None:
            filters[key] = value
    attributes = constraints_input.get("attributes", parsed.get("attributes"))
    if isinstance(attributes, list) and attributes:
        query = " ".join([str(query), *[str(item) for item in attributes]]).strip()
    normalized.update({
        "intent": action,
        "goal": goal or {"purchase_intent": "buy", "product_search": "find", "recommendation": "recommend", "product_comparison": "compare", "product_price": "inspect", "product_inventory": "inspect", "coupon_search": "inspect", "clarification": "discover"}.get(action),
        "subject": {"product_type": subject.get("product_type") or parsed.get("product_type"), "query": str(query).strip(), "brand": filters.get("brand"), "model": filters.get("model"), "reference": filters.get("reference"), "ean": filters.get("ean")},
        "constraints": {"budget_min": filters.get("budget_min"), "budget_max": filters.get("budget_max"), "attributes": filters.get("attributes") or [], "color": constraints_input.get("color"), "style": constraints_input.get("style")},
        "information_needed": parsed.get("information_needed") or ["catalog"],
        "needs_clarification": bool(parsed.get("needs_clarification")),
        "clarification_question": parsed.get("clarification_question"),
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


def _fallback_interpretation(text: str | None) -> SalesInterpretation:
    legacy = deterministic_scope(text)
    subject = legacy.get("subject") if isinstance(legacy.get("subject"), dict) else {}
    constraints = legacy.get("constraints") if isinstance(legacy.get("constraints"), dict) else {}
    filters = legacy.get("filters") if isinstance(legacy.get("filters"), dict) else {}
    fallback_goal = legacy.get("goal") or {
        "purchase_intent": "buy",
        "product_search": "find",
        "price": "inspect",
        "inventory": "inspect",
        "coupon": "inspect",
        "recommendation": "recommend",
        "product_comparison": "compare",
        "clarification": "discover",
    }.get(legacy.get("intent"))
    interpretation = SalesInterpretation(
        domain=legacy.get("domain", "out_of_scope"),
        goal=fallback_goal,
        subject={
            "product_type": subject.get("product_type") or legacy.get("product_type"),
            "brand": subject.get("brand") or filters.get("brand"),
            "model": subject.get("model") or filters.get("model"),
            "reference": subject.get("reference") or filters.get("reference"),
            "ean": subject.get("ean") or filters.get("ean"),
        },
        preferences={
            "budget_min": constraints.get("budget_min") or filters.get("budget_min"),
            "budget_max": constraints.get("budget_max") or filters.get("budget_max"),
            "color": constraints.get("color") or filters.get("color"),
            "style": constraints.get("style") or filters.get("style"),
            "attributes": constraints.get("attributes") or filters.get("attributes") or [],
        },
        references_previous_context=False,
        needs_clarification=bool(legacy.get("needs_clarification")),
        clarification_question=legacy.get("clarification_question"),
        confidence=0.6,
    )
    interpretation._source = "deterministic_fallback"
    return interpretation


def _log_interpretation(
    interpretation: SalesInterpretation,
    model: str,
    *,
    fallback_reason: str | None = None,
) -> None:
    preferences = interpretation.preferences
    payload = {
        "source": interpretation._source,
        "model": model,
        "domain": interpretation.domain,
        "goal": interpretation.goal,
        "confidence": interpretation.confidence,
        "references_previous_context": interpretation.references_previous_context,
        "has_product_type": bool(interpretation.subject.product_type),
        "has_brand": bool(interpretation.subject.brand),
        "has_style": bool(preferences.style),
        "has_color": bool(preferences.color),
        "has_budget": preferences.budget_min is not None or preferences.budget_max is not None,
        "needs_clarification": interpretation.needs_clarification,
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    print("[sales.interpreter]", payload)


def _normalize_interpreter_history(
    recent_turns: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _sanitize_openai_error_message(value: object) -> str:
    message = str(value or "OpenAI rejected the interpreter request")
    message = re.sub(r"sk-(?:proj-)?[A-Za-z0-9_-]+", "sk-***", message)
    message = re.sub(r"(?i)(authorization\s*[:=]?\s*bearer)\s+\S+", r"\1 ***", message)
    return message[:600]


def _bad_request_details(exc: BadRequestError, model: str) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    body_error = body.get("error") if isinstance(body.get("error"), dict) else body
    code = getattr(exc, "code", None) or body_error.get("code")
    param = getattr(exc, "param", None) or body_error.get("param")
    message = getattr(exc, "message", None) or body_error.get("message") or str(exc)
    return {
        "error_type": type(exc).__name__,
        "status_code": getattr(exc, "status_code", None),
        "error_code": code,
        "error_param": param,
        "error_message": _sanitize_openai_error_message(message),
        "model": model,
    }


def interpretation_to_plan(
    interpretation: SalesInterpretation,
    text: str | None = None,
) -> dict[str, Any]:
    subject = interpretation.subject.model_dump()
    preferences = interpretation.preferences.model_dump()
    if subject.get("reference"):
        query_parts = [str(subject["reference"])]
    elif subject.get("ean"):
        query_parts = [str(subject["ean"])]
    elif subject.get("brand") or subject.get("model"):
        query_parts = [str(value) for value in (subject.get("brand"), subject.get("model")) if value]
    elif subject.get("product_type"):
        query_parts = [str(subject["product_type"])]
    else:
        query_parts = []
    query = " ".join(query_parts).strip()

    resolved_action = resolve_commerce_action(text)
    goal_to_intent = {
        "discover": "clarification",
        "find": "product_search",
        "recommend": "recommendation",
        "compare": "product_comparison",
        "inspect": _ACTION_TO_PLAN.get(resolved_action or "", "product_search"),
        "buy": "purchase_intent",
        "after_sales": "clarification",
    }
    intent = "clarification" if interpretation.needs_clarification else goal_to_intent.get(
        interpretation.goal or "discover",
        "clarification",
    )
    filters = {
        key: value
        for key, value in {
            "brand": subject.get("brand"),
            "model": subject.get("model"),
            "reference": subject.get("reference"),
            "ean": subject.get("ean"),
            "budget_min": preferences.get("budget_min"),
            "budget_max": preferences.get("budget_max"),
            "attributes": preferences.get("attributes"),
            "color": preferences.get("color"),
            "style": preferences.get("style"),
        }.items()
        if value not in (None, [], "")
    }
    return {
        "domain": interpretation.domain,
        "intent": intent,
        "goal": interpretation.goal,
        "subject": {**subject, "query": query},
        "constraints": preferences,
        "query": query,
        "filters": filters,
        "budget_max": preferences.get("budget_max"),
        "product_type": subject.get("product_type"),
        "needs_clarification": interpretation.needs_clarification,
        "clarification_question": interpretation.clarification_question,
        "_source": interpretation._source,
    }


async def interpret_message(
    message: IncomingMessage,
    *,
    recent_turns: list[dict[str, str]] | None = None,
) -> SalesInterpretation:
    settings = get_settings()
    if _is_greeting(message.text):
        fallback = _fallback_interpretation(message.text)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="greeting_fast_path")
        return fallback
    if not settings.openai_api_key:
        fallback = _fallback_interpretation(message.text)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_api_key_missing")
        return fallback
    current_text = (message.text or "").strip()
    if not current_text:
        fallback = _fallback_interpretation(message.text)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="empty_message")
        return fallback

    normalized_history = _normalize_interpreter_history(recent_turns)
    messages = [
        {"role": "system", "content": SALES_INTERPRETER_INSTRUCTIONS},
        *normalized_history,
        {"role": "user", "content": current_text},
    ]
    print("[sales.interpreter.request]", {
        "model": settings.openai_model,
        "structured_output": True,
        "history_turns": len(normalized_history),
        "message_count": len(messages),
        "has_temperature": True,
        "has_max_tokens": False,
        "has_tools": False,
    })
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=messages,
            temperature=0,
            response_format=SalesInterpretation,
        )
        parsed_message = response.choices[0].message if response.choices else None
        if parsed_message is None or getattr(parsed_message, "refusal", None):
            raise ValueError("interpreter_refusal_or_empty_response")
        interpretation = getattr(parsed_message, "parsed", None)
        if not isinstance(interpretation, SalesInterpretation):
            raise ValueError("interpreter_schema_missing")
        interpretation._source = "openai"
        _log_interpretation(interpretation, settings.openai_model)
        return interpretation
    except BadRequestError as exc:
        print("[sales.interpreter.error]", _bad_request_details(exc, settings.openai_model))
        fallback = _fallback_interpretation(message.text)
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_bad_request")
        return fallback
    except (APIError, ValidationError, ValueError, TypeError) as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback_reason = "openai_request_failed" if isinstance(exc, APIError) else "openai_invalid_response"
        _log_interpretation(fallback, settings.openai_model, fallback_reason=fallback_reason)
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
    budget_max = None
    budget_match = re.search(r"(?:até|ate|por|no máximo|até o limite de)\s*(?:r\$\s*)?([\d.,]+)\s*(mil|k)?", query, flags=re.IGNORECASE)
    if budget_match:
        raw = budget_match.group(1).replace(".", "").replace(",", ".")
        budget_max = float(raw) * (1000 if budget_match.group(2) else 1)
        query = (query[:budget_match.start()] + query[budget_match.end():]).strip(" ,-")
    if query.lower().strip() in {"alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa"}:
        query = ""
    plan: dict[str, Any] = {
        "intent": "purchase_intent" if action == "purchase_intent" else _ACTION_TO_PLAN.get(action, "product_search"),
        "query": query,
        "filters": {"budget_max": budget_max} if budget_max is not None else {},
        "goal": "recommend" if budget_max is not None or (len(query.split()) > 1 and action == "purchase_intent") else ("buy" if action == "purchase_intent" else None),
        "subject": {"product_type": query.split()[0] if query else None, "query": query},
        "constraints": {"budget_max": budget_max, "attributes": query.split()[1:] if budget_max is not None and len(query.split()) > 1 else []},
    }
    if query and plan["intent"] in {"product_search", "price", "inventory", "recommendation"}:
        if len(query.split()) > 1 and not re.fullmatch(r"[A-Za-z0-9._/-]+", query):
            plan["filters"]["brand"] = _brand_from_query(query)
    brand = plan["filters"].get("brand")
    subject_model = " ".join(query.split()[1:]) if brand and len(query.split()) > 1 else None
    plan["subject"].update({"brand": brand, "model": subject_model})
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
    interpretation = await interpret_message(message)
    if interpretation.domain != "commerce":
        return None
    return interpretation_to_plan(interpretation, message.text)


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


def _fold(value: Any) -> str:
    import unicodedata

    text = str(value or "")
    return "".join(char for char in unicodedata.normalize("NFKD", text).lower() if not unicodedata.combining(char))


def _candidate_text(candidate: dict[str, Any]) -> str:
    fields = ("name", "brand", "model", "reference", "ean", "description", "category", "attributes", "color", "style")
    return _fold(" ".join(str(candidate.get(field) or "") for field in fields))


def _candidate_price(candidate: dict[str, Any]) -> float | None:
    for key in ("current_price", "promotional_price", "price"):
        value = candidate.get(key)
        try:
            if value is not None:
                if isinstance(value, str):
                    text = value.replace("R$", "").strip()
                    text = text.replace(".", "").replace(",", ".") if "," in text else text
                    return float(text)
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def score_candidate(candidate: dict[str, Any], plan: dict[str, Any]) -> float:
    subject = plan.get("subject") or {}
    constraints = plan.get("constraints") or {}
    text = _candidate_text(candidate)
    score = 0.0
    brand = _fold(subject.get("brand") or (plan.get("filters") or {}).get("brand"))
    model = _fold(subject.get("model") or (plan.get("filters") or {}).get("model"))
    reference = _fold(subject.get("reference") or (plan.get("filters") or {}).get("reference"))
    ean = _fold(subject.get("ean") or (plan.get("filters") or {}).get("ean"))
    query = _fold(subject.get("query") or plan.get("query"))
    if brand:
        if brand not in text:
            return float("-inf")
        score += 300
    if model:
        model_tokens = [token for token in model.split() if len(token) > 1]
        if model_tokens and not all(token in text for token in model_tokens):
            return float("-inf")
        score += 500
    if reference and reference not in text:
        return float("-inf")
    if reference:
        score += 1000
    if ean and ean not in text:
        return float("-inf")
    if ean:
        score += 1200
    query_tokens = [token for token in query.split() if len(token) > 2]
    score += sum(50 for token in query_tokens if token in text)
    attributes = constraints.get("attributes") or (plan.get("filters") or {}).get("attributes") or []
    for attribute in attributes if isinstance(attributes, list) else [attributes]:
        if _fold(attribute) in text:
            score += 40
    price = _candidate_price(candidate)
    budget_max = constraints.get("budget_max") or plan.get("budget_max") or (plan.get("filters") or {}).get("budget_max")
    if budget_max is not None and price is not None:
        try:
            if price > float(budget_max):
                return float("-inf")
            score += 80
        except (TypeError, ValueError):
            pass
    return score


def rank_candidates(candidates: list[dict[str, Any]], plan: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    ranked = [(score_candidate(candidate, plan), candidate) for candidate in candidates if isinstance(candidate, dict)]
    ranked = [(score, candidate) for score, candidate in ranked if score != float("-inf")]
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked[:limit]]


def _ranked_result(result: AgentResult, plan: dict[str, Any]) -> AgentResult | None:
    data = result.commercial_data or {}
    products = data.get("products") if isinstance(data.get("products"), list) else []
    selected = rank_candidates(products, plan)
    if not selected:
        return None
    from .commerce_router import _product_result

    action = "product_price" if plan.get("intent") == "price" else "product_search"
    ranked = _product_result(action, selected)
    if data.get("inventory") is not None:
        inventory = data["inventory"]
        ranked.reply_text = "Consulta de estoque:\n" + "\n".join(_product_lines(selected, inventory))
        ranked.commercial_data = {"products": selected, "inventory": inventory}
    return ranked


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
        return AgentResult(reply_text=html.unescape(content.strip()), intent="commerce", handoff_required=False)
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.responder] failed", {"error_type": type(exc).__name__})
        return None


async def handle_sales_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
) -> AgentResult | None:
    if isinstance(semantic_plan, SalesInterpretation):
        plan = interpretation_to_plan(semantic_plan, message.text)
    elif semantic_plan and semantic_plan.get("domain") == "commerce":
        plan = semantic_plan
    else:
        plan = await plan_sales_request(message)
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
    if plan.get("intent") == "clarification":
        question = plan.get("clarification_question") or "Claro. Está procurando relógio, acessório ou outro tipo de produto?"
        return AgentResult(reply_text=str(question), intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    if plan.get("intent") in {"purchase_intent", "recommendation"} and vague_query:
        return AgentResult(reply_text="Claro. Está procurando relógio, acessório ou outro tipo de produto?", intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    if plan.get("intent") == "product_search" and vague_query:
        return AgentResult(reply_text="Qual produto você quer encontrar? Informe o nome, modelo ou referência.", intent="commerce", handoff_required=False, safety_reason="commerce_clarification")
    constraints = plan.get("constraints") or {}
    if plan.get("intent") == "purchase_intent" and plan.get("query") and not any(constraints.get(key) for key in ("budget_min", "budget_max", "attributes", "color", "style")):
        return AgentResult(reply_text="Claro. Você procura algo mais esportivo, social ou casual? Tem alguma faixa de preço em mente?", intent="commerce", handoff_required=False, safety_reason="commerce_discovery")
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
    last_raw_result = None
    for attempt, query in enumerate(queries[:3], start=1):
        attempt_plan = {**plan, "query": query, "subject": {**(plan.get("subject") or {}), "query": query}}
        print("[sales.agent] tray_request", {"tool": "search_products", "attempt": attempt, "strategy": "initial" if attempt == 1 else "progressive"})
        raw_result = await handle_commerce_message(_planned_message(message, attempt_plan), facts, customer_context)
        last_raw_result = raw_result
        print("[sales.agent] tray_result", {"ok": raw_result is not None and raw_result.safety_reason != "tray_adapter_unavailable", "results_count": len((raw_result.commercial_data or {}).get("products", [])) if raw_result else 0})
        tray_result = _ranked_result(raw_result, attempt_plan) if raw_result else None
        if tray_result:
            print("[sales.agent] ranking", {"input_count": len((raw_result.commercial_data or {}).get("products", [])), "output_count": len((tray_result.commercial_data or {}).get("products", []))})
            break
        if raw_result and raw_result.safety_reason == "tray_adapter_unavailable":
            tray_result = raw_result
            break
        if raw_result and raw_result.safety_reason not in {"product_not_found", "ambiguous_product"}:
            tray_result = raw_result
            break
    if tray_result is None:
        tray_result = last_raw_result
    if tray_result is None:
        return None
    if plan.get("intent") in {"purchase_intent", "recommendation", "clarification"} and tray_result.safety_reason == "product_not_found":
        return AgentResult(reply_text="Não encontrei opções compatíveis no catálogo agora. Posso tentar outro tipo ou faixa de produto?", intent="commerce", handoff_required=False, safety_reason="recommendation_not_found")
    final = await _sales_response_with_openai(message, plan, tray_result)
    print("[sales.agent] responder", {"source": "openai" if final else "deterministic_fallback"})
    return final or tray_result
