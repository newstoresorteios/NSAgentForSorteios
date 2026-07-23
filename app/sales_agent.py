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
from .category_resolver import CategoryResolver
from .cart_service import (
    CartItemRequest,
    create_cart_checkout,
    create_cart_items_checkout,
    current_cart_reply,
    log_purchase_progress,
)
from .commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    evolve_commerce_state,
    product_reference_from_product,
    resolve_commerce_reference,
    resolve_purchase_item_reference,
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
from .payment_service import inspect_current_cart, inspect_payment_options
from .product_media import resolve_product_image
from .product_retrieval import (
    CUSTOMER_RESULT_LIMIT,
    commercial_availability_facts,
    ProductMatchError,
    ProductRetrievalCompiler,
    enrich_product_variants,
    exact_specific_product_matches,
    hard_filter_products,
    match_specific_products,
    prefilter_specific_candidates,
    product_availability_state,
    revalidate_products,
    rerank_products,
    semantic_preferences,
    specific_product_search_terms,
)
from .tray_tools import execute_tool


SALES_PLANNER_INSTRUCTIONS = """
Você planeja consultas comerciais para a New Store. Retorne somente JSON válido.
Use este formato: domain, goal, subject, constraints, information_needed,
enough_information_to_search, ready_for_retrieval, stop_clarification,
needs_clarification e clarification_question.
goal deve ser discover, find, recommend, compare, inspect, buy ou after_sales.
subject deve conter product_type, query, brand, model, reference e ean.
constraints deve conter budget_min, budget_max, attributes, color, style, material e
explicit_no_preferences.
Não produza fatos comerciais nem diga que um produto existe.
""".strip()

SALES_RESPONDER_INSTRUCTIONS = """
Você é um vendedor objetivo e prestativo da New Store.
Use exclusivamente os fatos comerciais retornados pelo TrayAdapter no bloco FACTS.
Não invente produto, preço, estoque, promoção, disponibilidade, Pix, parcelamento ou cupom.
Se um fato não estiver em FACTS, diga que não foi informado.
Responda em português do Brasil, de forma curta para WhatsApp.
Apresente normalmente no máximo três opções relevantes. Não termine toda resposta
automaticamente com outra pergunta; deixe o cliente reagir quando os produtos já foram apresentados.
Quando FACTS contiver uma lista de produtos, preserve a ordem recebida e numere as opções
como 1, 2 e 3. Não altere essa ordem, pois ela será usada nas referências posteriores.
Quando FACTS.match_status for ambiguous, apresente as correspondências plausíveis e peça
ao cliente para identificar qual delas pretendia, sem escolher uma arbitrariamente.
Quando FACTS contiver cart_url, use somente esse link oficial. Nunca peça número completo
do cartão, CVV, senha, código ou validade pelo WhatsApp; o pagamento termina no checkout.
Preferências do cliente no plano não são fatos confirmados do produto. Só afirme material,
cor, dimensões ou adequação física quando esses dados estiverem presentes em FACTS.
Nunca transforme uma preferência desejada em característica do item. Para recomendar por
medida corporal, use dimensões reais presentes no nome, propriedades ou descrição factual.
Estoque positivo, sozinho, não significa pronta entrega. Só afirme entrega imediata quando
commercial_availability.immediate_delivery_supported nos FACTS for igual a true. Se houver prazo,
informe o prazo comercial e não o contradiga com uma promessa de pronta entrega.
""".strip()

SALES_CLARIFICATION_INSTRUCTIONS = """
Você é um vendedor da NewStore no WhatsApp.
Faça uma resposta curta para obter no máximo DUAS informações relacionadas que
realmente mudariam a busca. Considere o histórico, a interpretação e DISCOVERY_STATE.
Não transforme a conversa em questionário. Não pergunte novamente informação já
fornecida, presente em known_preferences ou em recent_questions. Não pergunte por uma
preferência listada em explicit_no_preferences; isso significa que o cliente disse que
não possui preferência naquele critério.
Não afirme produto, preço, estoque, promoção ou condição comercial, pois a Tray ainda
não foi consultada. Responda apenas com uma frase curta ou até duas perguntas simples
e relacionadas.
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
interpretação entre 0 e 1. Em information_needed, indique somente os fatos necessários:
catalog, price, inventory, coupons ou payment.

Decida também:
- enough_information_to_search=true quando já existe produto/categoria identificável e
  informação suficiente para iniciar uma busca útil. Uma preferência relevante costuma
  bastar; não exija cor, material, estilo, tamanho, marca e funções ao mesmo tempo.
- ready_for_retrieval=true quando o cliente pede semanticamente para ver, buscar ou receber
  opções/catálogo agora.
- stop_clarification=true quando o cliente demonstra atrito, pede para agir, diz que já
  respondeu, não sabe, não tem preferência ou quer encerrar as perguntas.
- preferences.explicit_no_preferences deve listar os critérios em que o cliente declarou
  não ter preferência, usando somente os nomes canônicos budget, brand, color, style,
  material, occasion, recipient ou attributes. null significa apenas desconhecido.

Mensagens curtas podem atualizar uma preferência anterior. Quando houver mudança, a
preferência explícita mais recente vence; não mantenha o valor substituído em attributes.
Se ready_for_retrieval ou stop_clarification for true e houver subject identificável,
needs_clarification deve ser false.
Quando needs_clarification=true, clarification_question deve conter uma frase curta com
no máximo duas perguntas relacionadas e não pode repetir algo já respondido no histórico.

COMMERCE_STATE contém contexto semântico confiável da conversa, incluindo produto ativo,
lista mais recente apresentada, tópico e etapa de compra. Use esse estado para interpretar
expressões como "o terceiro", "esse", "o que você recomendou" e continuações curtas.
Nunca copie nem invente product_id ou variant_id.
- reference_type=list_position e reference_position=N para posição numerada;
- reference_type=current_product para "esse produto" quando há produto ativo;
- reference_type=previous_recommendation para a recomendação principal;
- reference_type=last_presented_product para o último item apresentado;
- reference_type=explicit_product quando o nome/modelo citado corresponde à lista.
Defina active_topic para o conceito em discussão, sem confundir palavras ambíguas com
outro domínio. Se active_domain=commerce, interprete mensagens ambíguas primeiro nesse
contexto. domain_change_explicit=true somente quando o cliente mudar claramente de
assunto. Perguntas sobre pagamento de um produto continuam em commerce e usam
purchase_stage=payment_discussion.
Interprete semanticamente a etapa de carrinho:
- purchase_action=create_cart quando o cliente confirma que quer levar um produto
  identificado; use reference_type/reference_position para indicar qual produto;
- purchase_action=show_cart_link quando pede novamente o link do carrinho atual;
- purchase_action=checkout_question quando pergunta como ou onde concluir o pagamento.
- purchase_action=inspect_cart quando pergunta o total ou os itens do carrinho atual.
Para comprar vários produtos, preencha purchase_items com uma entrada para cada item,
preservando referência semântica e quantidade. Não invente IDs. Use list_position para
itens numerados, current_product para o produto ativo e explicit_product com o nome citado.
Defina image_request=true quando pedir imagem/foto do produto referenciado.
Uma mensagem pode combinar payment_action e purchase_action. Quando o cliente confirmar
que quer comprar um produto identificado e escolher como pagar, preserve payment_action
e defina purchase_action=create_cart no mesmo resultado. Nao deixe a intencao de
pagamento apagar o compromisso de compra.
Use payment_method_preference somente quando o cliente escolher ou declarar preferencia
por pix, card, boleto ou other; uma pergunta geral sobre aceitacao nao e uma escolha.
COMMERCE_STATE.pending_action representa uma acao concreta oferecida imediatamente antes.
Defina confirmation=confirm quando a mensagem atual aceitar semanticamente essa acao,
confirmation=reject quando recusar e confirmation=none quando nao responder a ela.
Nao dependa de uma palavra exata. Se confirmar create_cart/confirm_purchase, preserve
goal=buy e purchase_action=create_cart. Se mudar de produto ou assunto, nao confirme a
acao anterior.
Se o assistente pediu uma escolha factual de variante para concluir pending_action=create_cart
e o cliente fornecer essa preferencia, use confirmation=none, preserve a preferencia
estruturada e mantenha purchase_action=create_cart para continuar a mesma compra.
Defina payment_action=payment_options para formas de pagamento e payment_action=installment
quando pedir uma quantidade de parcelas; nesse caso extraia installment_count.
Extraia quantity como inteiro positivo quando o cliente informar quantidade. Caso não
informe, deixe quantity=null. Nunca invente product_id, variant_id, session_id ou cart_url.
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
        "constraints": {
            "budget_min": filters.get("budget_min"),
            "budget_max": filters.get("budget_max"),
            "attributes": filters.get("attributes") or [],
            "color": constraints_input.get("color"),
            "style": constraints_input.get("style"),
            "material": constraints_input.get("material"),
            "explicit_no_preferences": constraints_input.get("explicit_no_preferences") or [],
        },
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
            "material": constraints.get("material") or filters.get("material"),
            "attributes": constraints.get("attributes") or filters.get("attributes") or [],
            "explicit_no_preferences": constraints.get("explicit_no_preferences") or [],
        },
        information_needed=["catalog"] if legacy.get("domain") == "commerce" else [],
        references_previous_context=False,
        enough_information_to_search=False,
        ready_for_retrieval=False,
        stop_clarification=False,
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
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "needs_clarification": interpretation.needs_clarification,
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    print("[sales.interpreter]", payload)


def _normalize_interpreter_history(
    recent_turns: list[dict[str, Any]] | None,
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

    information_needed = set(interpretation.information_needed)
    inspect_intent = (
        "inventory" if "inventory" in information_needed
        else "coupon" if "coupons" in information_needed
        else "price" if information_needed.intersection({"price", "payment"})
        else "product_search"
    )
    goal_to_intent = {
        "discover": "clarification",
        "find": "product_search",
        "recommend": "recommendation",
        "compare": "product_comparison",
        "inspect": inspect_intent,
        "buy": "purchase_intent",
        "after_sales": "clarification",
    }
    retrieval_signal = any((
        interpretation.enough_information_to_search,
        interpretation.ready_for_retrieval,
        interpretation.stop_clarification,
    ))
    if retrieval_signal and interpretation.goal in {"discover", "recommend", "buy"}:
        intent = "recommendation"
    else:
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
            "material": preferences.get("material"),
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
        "information_needed": interpretation.information_needed,
        "enough_information_to_search": interpretation.enough_information_to_search,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "purchase_action": interpretation.purchase_action,
        "quantity": interpretation.quantity,
        "purchase_items": [
            item.model_dump(mode="json")
            for item in interpretation.purchase_items
        ],
        "image_request": interpretation.image_request,
        "payment_action": interpretation.payment_action,
        "payment_method_preference": interpretation.payment_method_preference,
        "confirmation": interpretation.confirmation,
        "installment_count": interpretation.installment_count,
        "_source": interpretation._source,
    }


async def interpret_message(
    message: IncomingMessage,
    *,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> SalesInterpretation:
    settings = get_settings()
    if _is_greeting(message.text):
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "greeting_fast_path"
        _log_interpretation(fallback, settings.openai_model, fallback_reason="greeting_fast_path")
        return fallback
    if not settings.openai_api_key:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "openai_api_key_missing"
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_api_key_missing")
        return fallback
    current_text = (message.text or "").strip()
    if not current_text:
        fallback = _fallback_interpretation(message.text)
        fallback._fallback_reason = "empty_message"
        _log_interpretation(fallback, settings.openai_model, fallback_reason="empty_message")
        return fallback

    normalized_history = _normalize_interpreter_history(recent_turns)
    state_message = {
        "role": "system",
        "content": "COMMERCE_STATE:\n" + json.dumps(
            (commerce_state or CommerceConversationState()).interpreter_payload(),
            ensure_ascii=False,
        ),
    }
    messages = [
        {"role": "system", "content": SALES_INTERPRETER_INSTRUCTIONS},
        state_message,
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
        fallback._fallback_reason = "openai_bad_request"
        _log_interpretation(fallback, settings.openai_model, fallback_reason="openai_bad_request")
        return fallback
    except (APIError, ValidationError, ValueError, TypeError) as exc:
        print("[sales.interpreter] failed", {"error_type": type(exc).__name__})
        fallback = _fallback_interpretation(message.text)
        fallback_reason = "openai_request_failed" if isinstance(exc, APIError) else "openai_invalid_response"
        fallback._fallback_reason = fallback_reason
        _log_interpretation(fallback, settings.openai_model, fallback_reason=fallback_reason)
        return fallback


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
    ean_match = re.fullmatch(r"(?:ean\s+)?(\d{8,14})", query, flags=re.IGNORECASE)
    reference = None
    if not ean_match and query and (
        re.search(r"[./_-]", query)
        or (re.search(r"\d", query) and re.search(r"[A-Za-z]", query) and " " not in query)
    ):
        reference = re.sub(r"^(?:sku|ref(?:er[êe]ncia)?)\s+", "", query, flags=re.IGNORECASE)
    fallback_product_type = None
    fallback_model = None
    if query and not ean_match and not reference:
        if action == "product_search":
            fallback_model = query
        else:
            fallback_product_type = query.split()[0] if action == "purchase_intent" else query
    plan: dict[str, Any] = {
        "intent": "purchase_intent" if action == "purchase_intent" else _ACTION_TO_PLAN.get(action, "product_search"),
        "query": query,
        "filters": {"budget_max": budget_max} if budget_max is not None else {},
        "goal": "recommend" if budget_max is not None or (len(query.split()) > 1 and action == "purchase_intent") else ("buy" if action == "purchase_intent" else None),
        "subject": {
            "product_type": fallback_product_type,
            "query": query,
            "ean": ean_match.group(1) if ean_match else None,
            "reference": reference,
        },
        "constraints": {"budget_max": budget_max, "attributes": query.split()[1:] if budget_max is not None and len(query.split()) > 1 else []},
    }
    plan["subject"].update({"brand": None, "model": fallback_model})
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


def _mark_sales_result(
    result: AgentResult,
    *,
    interpretation: SalesInterpretation | None,
    goal: str | None,
    response_source: str,
    used_openai_responder: bool,
    used_tray: bool,
    fallback_reason: str | None = None,
) -> AgentResult:
    interpreter_source = interpretation._source if interpretation else None
    marked = result.with_response_metadata(
        domain="commerce",
        goal=goal,
        response_source=response_source,
        used_openai_interpreter=interpreter_source == "openai",
        used_openai_responder=used_openai_responder,
        used_tray=used_tray,
        fallback_reason=fallback_reason or (interpretation._fallback_reason if interpretation else None),
    )
    if interpretation is not None:
        if interpretation._clear_pending_action:
            marked.response_metadata["clear_pending_action"] = True
        marked.response_metadata.setdefault("active_topic", interpretation.active_topic)
        marked.response_metadata.setdefault("purchase_stage", interpretation.purchase_stage)
        marked.response_metadata.setdefault(
            "active_preferences",
            interpretation.preferences.model_dump(mode="json", exclude_none=True),
        )
    return marked


CLARIFICATION_BUDGET = 2


def _is_clarification_turn(turn: dict[str, Any]) -> bool:
    metadata = turn.get("metadata") if isinstance(turn, dict) else None
    return (
        turn.get("role") == "assistant"
        and isinstance(metadata, dict)
        and metadata.get("safety_reason") == "commerce_clarification"
    )


def _consecutive_clarification_count(recent_turns: list[dict[str, Any]] | None) -> int:
    count = 0
    for turn in reversed(recent_turns or []):
        if turn.get("role") == "user":
            continue
        if not _is_clarification_turn(turn):
            break
        count += 1
    return count


def _known_preferences(interpretation: SalesInterpretation) -> dict[str, Any]:
    preferences = interpretation.preferences
    known: dict[str, Any] = {}
    if preferences.budget_min is not None or preferences.budget_max is not None:
        known["budget"] = {
            "min": preferences.budget_min,
            "max": preferences.budget_max,
        }
    for field in ("color", "style", "material", "occasion", "recipient"):
        value = getattr(preferences, field)
        if value:
            known[field] = value
    if interpretation.subject.brand:
        known["brand"] = interpretation.subject.brand
    if preferences.attributes:
        known["attributes"] = preferences.attributes
    return known


def _subject_identifiable(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    return any((subject.product_type, subject.brand, subject.model, subject.reference, subject.ean))


def _discovery_state(
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    clarification_count = _consecutive_clarification_count(recent_turns)
    known_preferences = _known_preferences(interpretation)
    explicit_no_preferences = list(dict.fromkeys(interpretation.preferences.explicit_no_preferences))
    known_preferences_count = len(known_preferences) + len(explicit_no_preferences)
    subject_identifiable = _subject_identifiable(interpretation)
    enough_information = interpretation.enough_information_to_search or (
        subject_identifiable and known_preferences_count > 0
    )
    budget_remaining = max(0, CLARIFICATION_BUDGET - clarification_count)
    force_retrieval = subject_identifiable and any((
        enough_information,
        budget_remaining == 0,
        interpretation.ready_for_retrieval,
        interpretation.stop_clarification,
    ))
    recent_questions = [
        str(turn.get("content") or "").strip()
        for turn in recent_turns or []
        if _is_clarification_turn(turn) and str(turn.get("content") or "").strip()
    ][-CLARIFICATION_BUDGET:]
    preference_fields = {"budget", "brand", "color", "style", "material", "occasion", "recipient", "attributes"}
    unknown_preferences = sorted(
        preference_fields - set(known_preferences) - set(explicit_no_preferences)
    )
    return {
        "clarification_count": clarification_count,
        "clarification_budget_remaining": budget_remaining,
        "enough_information_to_search": enough_information,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "known_preferences": known_preferences,
        "known_preferences_count": known_preferences_count,
        "unknown_preferences": unknown_preferences,
        "explicit_no_preferences": explicit_no_preferences,
        "recent_questions": recent_questions,
        "subject_identifiable": subject_identifiable,
        "force_retrieval": force_retrieval,
    }


def _needs_clarification_before_retrieval(
    interpretation: SalesInterpretation,
    plan: dict[str, Any],
    discovery_state: dict[str, Any],
) -> bool:
    if discovery_state["force_retrieval"]:
        return False
    if interpretation.needs_clarification or interpretation.goal == "discover":
        return True
    if plan.get("intent") not in {"purchase_intent", "recommendation"}:
        return False
    return not discovery_state["subject_identifiable"]


async def generate_clarification_reply(
    *,
    message: IncomingMessage,
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None = None,
    context_note: str | None = None,
    used_tray: bool = False,
    discovery_state: dict[str, Any] | None = None,
) -> AgentResult:
    settings = get_settings()
    deterministic_question = (
        interpretation.clarification_question
        or "Qual característica ou preferência é mais importante para você?"
    )
    if interpretation._source == "openai" and interpretation.clarification_question:
        return _mark_sales_result(
            AgentResult(
                reply_text=html.unescape(interpretation.clarification_question.strip()),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="openai",
            used_openai_responder=False,
            used_tray=used_tray,
        )
    if not settings.openai_api_key:
        return _mark_sales_result(
            AgentResult(
                reply_text=deterministic_question,
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=used_tray,
            fallback_reason="openai_api_key_missing",
        )

    normalized_history = _normalize_interpreter_history(recent_turns)
    request_context = {
        "current_message": message.text,
        "interpretation": interpretation.model_dump(),
        "context_note": context_note,
        "DISCOVERY_STATE": discovery_state or _discovery_state(interpretation, recent_turns),
    }
    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SALES_CLARIFICATION_INSTRUCTIONS},
                *normalized_history,
                {"role": "user", "content": json.dumps(request_context, ensure_ascii=False)},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            raise ValueError("clarification_response_empty")
        return _mark_sales_result(
            AgentResult(
                reply_text=html.unescape(content.strip()),
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="openai",
            used_openai_responder=True,
            used_tray=used_tray,
        )
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.clarification] failed", {"error_type": type(exc).__name__})
        return _mark_sales_result(
            AgentResult(
                reply_text=deterministic_question,
                intent="commerce",
                handoff_required=False,
                safety_reason="commerce_clarification",
            ),
            interpretation=interpretation,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=used_tray,
            fallback_reason="clarification_responder_failed",
        )


async def _sales_response_with_openai(
    message: IncomingMessage,
    plan: dict[str, Any],
    tray_result: AgentResult,
    interpretation: SalesInterpretation | None = None,
) -> AgentResult | None:
    settings = get_settings()
    if not settings.openai_api_key or tray_result.safety_reason in {
        "tray_adapter_unavailable", "product_match_failed", "product_not_found",
        "ambiguous_product", "product_context_missing", "coupon_not_found",
        "cart_technical_failure", "cart_validation_error",
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
                        {
                            "original_message": message.text,
                            "plan": plan,
                            "FACTS": tray_result.commercial_data or {"summary": tray_result.reply_text},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            return None
        final_result = AgentResult(
            reply_text=html.unescape(content.strip()),
            intent="commerce",
            handoff_required=False,
            safety_reason=tray_result.safety_reason,
            commercial_data=tray_result.commercial_data,
            response_metadata=dict(tray_result.response_metadata),
        )
        return _mark_sales_result(
            final_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="openai",
            used_openai_responder=True,
            used_tray=bool(tray_result.response_metadata.get("used_tray", True)),
        )
    except (APIError, ValueError, TypeError) as exc:
        print("[sales.responder] failed", {"error_type": type(exc).__name__})
        return None


async def _execute_contextual_product_lookup(
    interpretation: SalesInterpretation,
    product_reference: CommerceProductReference,
) -> AgentResult:
    product_id = product_reference.product_id
    print("[sales.product.resolve]", {
        "strategy": "context",
        "has_brand": bool(product_reference.brand),
        "has_model": False,
        "candidate_count": 1,
        "matched_count": 1,
    })
    current = await execute_tool("get_product", {"product_id": product_id})
    if "error" in current:
        return AgentResult(
            reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
            intent="commerce",
            handoff_required=False,
            safety_reason="tray_adapter_unavailable",
        )
    product = {
        key: value
        for key, value in {
            "id": product_id,
            "name": product_reference.name,
            "reference": product_reference.reference,
            "ean": product_reference.ean,
            "brand": product_reference.brand,
        }.items()
        if value is not None
    }
    product.update(current)
    inventory: dict[str, Any] | None = None
    if "inventory" in interpretation.information_needed:
        inventory = await execute_tool("check_inventory", {"product_id": product_id})
        if "error" in inventory:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
    enriched = await enrich_product_variants([product], interpretation, execute_tool)
    availability_input = {
        **enriched[0],
        **(inventory or {}),
    }
    availability_facts = commercial_availability_facts(availability_input)
    enriched[0]["commercial_availability"] = availability_facts
    print("[sales.availability.fact]", {
        "has_stock": availability_facts["has_stock"],
        "has_lead_time": availability_facts["has_lead_time"],
        "immediate_delivery_supported": availability_facts["immediate_delivery_supported"],
    })
    availability_state = product_availability_state(enriched[0])
    print("[sales.product.availability]", {
        "resolved": True,
        "available_state": availability_state,
    })
    if availability_state == "unavailable":
        return AgentResult(
            reply_text=(
                "Encontrei esse modelo no catálogo, mas ele está indisponível no momento. "
                "Posso procurar outras versões dele ou modelos semelhantes."
            ),
            intent="commerce",
            handoff_required=False,
            safety_reason="product_unavailable",
            commercial_data={
                "products": enriched,
                "availability_state": availability_state,
            },
            response_metadata={
                "active_product": product_reference.model_dump(mode="json"),
                "presented_products": False,
                "product_resolution_state": "found_unavailable",
            },
        )
    from .commerce_router import _product_result

    result = _product_result("product_search", enriched)
    if inventory is not None:
        result.commercial_data = {
            "products": enriched,
            "inventory": inventory,
        }
    result.response_metadata.update({
        "active_product": product_reference.model_dump(mode="json"),
        "presented_products": False,
        "product_resolution_state": (
            "found_available" if availability_state == "available" else "found_unknown"
        ),
    })
    return result


async def _execute_compiled_product_retrieval(
    interpretation: SalesInterpretation,
) -> AgentResult | None:
    initial_plan = ProductRetrievalCompiler.compile(interpretation)
    category_resolution = None
    if (
        initial_plan.mode == "recommendation"
        and interpretation.subject.product_type
    ) or (
        initial_plan.mode == "exact"
        and interpretation.subject.product_type
        and not interpretation.subject.brand
        and not interpretation.subject.reference
        and not interpretation.subject.ean
    ):
        category_resolution = await CategoryResolver(execute_tool).resolve(
            interpretation.subject.product_type
        )
    retrieval_plan = ProductRetrievalCompiler.compile(
        interpretation,
        category_ids=(category_resolution.product_category_ids if category_resolution else ()),
    )
    preferences = semantic_preferences(interpretation)
    has_budget = any((
        interpretation.preferences.budget_min is not None,
        interpretation.preferences.budget_max is not None,
    ))
    print("[sales.retrieval.plan]", {
        "goal": interpretation.goal,
        "has_product_type": bool(interpretation.subject.product_type),
        "has_brand": bool(interpretation.subject.brand),
        "has_model": bool(interpretation.subject.model),
        "has_budget": has_budget,
        "semantic_preferences_count": len(preferences),
        "candidate_limit": retrieval_plan.candidate_limit,
    })
    if not retrieval_plan.requests:
        return None

    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    hard_filtered: list[dict[str, Any]] = []
    product_lookup_failed = False
    specific_resolution = None
    used_brand_candidates = False
    used_category_candidates = False
    search_term_count = len(specific_product_search_terms(interpretation))
    catalog_discovered_count = 0
    for request in retrieval_plan.requests:
        catalog_discovery = (
            retrieval_plan.mode == "exact"
            and request.strategy in {"brand_candidates", "category_candidates"}
        )
        pages = (
            range(1, retrieval_plan.discovery_max_pages + 1)
            if catalog_discovery
            else (request.page,)
        )
        for page in pages:
            page_limit = (
                retrieval_plan.discovery_page_limit
                if catalog_discovery
                else request.limit
            )
            arguments = {
                **request.tool_arguments(),
                "limit": page_limit,
                "page": page,
            }
            print("[sales.retrieval.request]", {
                "strategy": request.strategy,
                "category_id_present": bool(request.category_id),
                "name_filter_present": bool(request.name),
                "has_brand_filter": bool(request.brand),
                "has_budget_filter": has_budget,
                "candidate_limit": page_limit,
            })
            result = await execute_tool("search_products", arguments)
            used_brand_candidates = (
                used_brand_candidates or request.strategy == "brand_candidates"
            )
            used_category_candidates = (
                used_category_candidates or request.strategy == "category_candidates"
            )
            if "error" in result:
                product_lookup_failed = True
                break
            raw_products = (
                result.get("products")
                if isinstance(result.get("products"), list)
                else []
            )
            accumulation_limit = (
                retrieval_plan.discovery_max_products
                + retrieval_plan.candidate_limit
                if retrieval_plan.mode == "exact"
                else retrieval_plan.candidate_limit
            )
            for product in raw_products:
                if not isinstance(product, dict) or product.get("id") is None:
                    continue
                product_id = str(product["id"])
                if product_id in seen_ids:
                    continue
                seen_ids.add(product_id)
                candidates.append(product)
                if catalog_discovery:
                    catalog_discovered_count += 1
                if len(candidates) >= accumulation_limit:
                    break
            if retrieval_plan.mode == "exact":
                hard_filtered = exact_specific_product_matches(
                    candidates,
                    interpretation,
                )
            else:
                hard_filtered = hard_filter_products(
                    candidates,
                    interpretation,
                    mode=retrieval_plan.mode,
                )
            print("[sales.retrieval.result]", {
                "strategy": request.strategy,
                "raw_candidate_count": len(raw_products),
                "hard_filtered_count": len(hard_filtered),
            })
            strategy = (
                "reference" if request.reference
                else "ean" if request.ean
                else "brand_candidates" if request.strategy == "brand_candidates"
                else "category_candidates" if request.category_id
                else "model" if interpretation.subject.model
                else "name"
            )
            print("[sales.product.resolve]", {
                "strategy": strategy,
                "has_brand": bool(interpretation.subject.brand),
                "has_model": bool(interpretation.subject.model),
                "candidate_count": len(candidates),
                "matched_count": len(hard_filtered),
            })
            if catalog_discovery:
                paging = (
                    result.get("paging")
                    if isinstance(result.get("paging"), dict)
                    else {}
                )
                try:
                    total = int(paging["total"]) if paging.get("total") is not None else None
                except (TypeError, ValueError):
                    total = None
                try:
                    response_limit = int(paging.get("limit") or page_limit)
                except (TypeError, ValueError):
                    response_limit = page_limit
                consumed = page * max(response_limit, 1)
                has_more = bool(raw_products) and (
                    consumed < total
                    if total is not None
                    else len(raw_products) >= page_limit
                )
                print("[sales.catalog.discovery]", {
                    "strategy": (
                        "brand" if request.strategy == "brand_candidates"
                        else "category"
                    ),
                    "brand_present": bool(request.brand),
                    "category_present": bool(request.category_id),
                    "search_term_count": search_term_count,
                    "page": page,
                    "limit": page_limit,
                    "returned_count": len(raw_products),
                    "accumulated_count": catalog_discovered_count,
                    "total_if_known": total,
                })
                if (
                    hard_filtered
                    or not has_more
                    or catalog_discovered_count
                    >= retrieval_plan.discovery_max_products
                ):
                    break
            else:
                break
        if retrieval_plan.mode == "recommendation" and (
            len(candidates) >= retrieval_plan.candidate_limit
        ):
            break
        if retrieval_plan.mode == "exact" and hard_filtered:
            break

    if retrieval_plan.mode == "exact" and candidates:
        matcher_candidates = prefilter_specific_candidates(
            candidates,
            interpretation,
            limit=retrieval_plan.candidate_limit,
        )
        print("[sales.catalog.prefilter]", {
            "discovered_count": len(candidates),
            "shortlisted_count": len(matcher_candidates),
        })
        try:
            specific_resolution = await match_specific_products(
                matcher_candidates,
                interpretation,
            )
            hard_filtered = list(specific_resolution.products)
        except ProductMatchError:
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_match_failed",
            )
        print("[sales.product.disambiguation]", {
            "candidate_pool_count": len(matcher_candidates),
            "plausible_count": len(hard_filtered),
            "match_status": specific_resolution.status,
            "used_brand_candidates": used_brand_candidates,
            "used_category_candidates": used_category_candidates,
        })

    if not candidates:
        if category_resolution and category_resolution.lookup_failed:
            category_failure = (
                category_resolution.failure_reason or "category_adapter_error"
            )
            print("[sales.retrieval.empty]", {"reason": category_failure})
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason=category_failure,
            )
        if product_lookup_failed:
            print("[sales.retrieval.empty]", {"reason": "catalog_lookup_failed"})
            return AgentResult(
                reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                intent="commerce",
                handoff_required=False,
                safety_reason="tray_adapter_unavailable",
            )
        reason = "exact_product_not_found" if retrieval_plan.mode == "exact" else "catalog_empty"
        print("[sales.retrieval.empty]", {"reason": reason})
        if retrieval_plan.mode == "exact":
            return AgentResult(
                reply_text="Não encontrei esse produto no catálogo agora.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_not_found",
            )
        return AgentResult(
            reply_text="Não encontrei opções disponíveis para esses critérios agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="recommendation_no_match",
        )
    if not hard_filtered:
        reason = "exact_product_not_found" if retrieval_plan.mode == "exact" else "hard_filter_empty"
        print("[sales.retrieval.empty]", {"reason": reason})
        if retrieval_plan.mode == "exact":
            if product_lookup_failed:
                return AgentResult(
                    reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="tray_adapter_unavailable",
                )
            return AgentResult(
                reply_text="Não encontrei esse produto no catálogo agora.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_not_found",
            )
        return AgentResult(
            reply_text="Encontrei produtos no catálogo, mas nenhum atende aos critérios objetivos informados agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="recommendation_no_match",
        )

    if retrieval_plan.mode == "recommendation":
        enriched = await enrich_product_variants(
            hard_filtered,
            interpretation,
            execute_tool,
        )
        ranked = await rerank_products(enriched, interpretation)
    else:
        ranked = hard_filtered
    selected = ranked[:CUSTOMER_RESULT_LIMIT]
    refreshed, revalidation_failed = await revalidate_products(
        selected,
        interpretation,
        execute_tool,
    )
    if not refreshed and revalidation_failed:
        return AgentResult(
            reply_text="Não consegui consultar as informações da loja neste momento. Tente novamente em instantes.",
            intent="commerce",
            handoff_required=False,
            safety_reason="tray_adapter_unavailable",
        )
    from .commerce_router import _product_result

    final_products = refreshed or selected
    if retrieval_plan.mode == "exact":
        final_products = [
            {
                **product,
                "availability_state": product_availability_state(product),
            }
            for product in final_products
        ]
        availability_states = [
            str(product["availability_state"])
            for product in final_products
        ]
        if any(state == "available" for state in availability_states):
            availability_state = "available"
        elif availability_states and all(state == "unavailable" for state in availability_states):
            availability_state = "unavailable"
        else:
            availability_state = "unknown"
        print("[sales.product.availability]", {
            "resolved": bool(final_products),
            "available_state": availability_state,
        })
        if specific_resolution and specific_resolution.status == "ambiguous":
            result = _product_result("product_disambiguation", final_products)
            result.commercial_data = {
                "products": final_products,
                "match_status": "ambiguous",
            }
            result.response_metadata.update({
                "presented_products": True,
                "product_resolution_state": "plausible_matches",
                "clear_active_product": True,
            })
            return result
        if availability_state == "unavailable":
            return AgentResult(
                reply_text=(
                    "Encontrei esse modelo no catálogo, mas ele está indisponível no momento. "
                    "Posso procurar outras versões dele ou modelos semelhantes."
                ),
                intent="commerce",
                handoff_required=False,
                safety_reason="product_unavailable",
                commercial_data={
                    "products": final_products,
                    "availability_state": availability_state,
                },
                response_metadata={
                    "presented_products": True,
                    "product_resolution_state": "found_unavailable",
                },
            )
    result = _product_result("product_search", final_products)
    result.response_metadata["presented_products"] = True
    if retrieval_plan.mode == "exact":
        result.response_metadata["product_resolution_state"] = (
            "found_available" if availability_state == "available" else "found_unknown"
        )
        if result.commercial_data is not None:
            result.commercial_data["availability_state"] = availability_state
    return result


async def _ensure_cart_for_purchase(
    *,
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
    purchase_requests: list[CartItemRequest],
    resolved_product: CommerceProductReference | None,
) -> tuple[CommerceConversationState, AgentResult | None]:
    if state.cart_session_id:
        print("[sales.purchase.ensure_cart]", {
            "cart_existed": True,
            "cart_created": False,
            "item_count": len(state.cart_items),
        })
        return state, None

    if purchase_requests:
        cart_result = await create_cart_items_checkout(
            item_requests=purchase_requests,
            state=state,
            execute=execute_tool,
        )
    elif resolved_product is not None:
        cart_result = await create_cart_checkout(
            interpretation=interpretation,
            product_reference=resolved_product,
            state=state,
            execute=execute_tool,
        )
    else:
        print("[sales.purchase.ensure_cart]", {
            "cart_existed": False,
            "cart_created": False,
            "item_count": 0,
        })
        return state, None

    updated_state = evolve_commerce_state(state, cart_result)
    print("[sales.purchase.ensure_cart]", {
        "cart_existed": False,
        "cart_created": bool(updated_state.cart_session_id),
        "item_count": len(updated_state.cart_items),
    })
    return updated_state, cart_result


def _combine_cart_and_payment_results(
    cart_result: AgentResult,
    payment_result: AgentResult,
) -> AgentResult:
    commercial_data = dict(cart_result.commercial_data or {})
    for key, value in (payment_result.commercial_data or {}).items():
        if key == "cart" and key in commercial_data:
            continue
        commercial_data[key] = value
    metadata = dict(cart_result.response_metadata or {})
    metadata.update(payment_result.response_metadata or {})
    if "cart_state" in cart_result.response_metadata:
        metadata["cart_state"] = cart_result.response_metadata["cart_state"]
    metadata["purchase_stage"] = "payment_discussion"

    reply = payment_result.reply_text
    cart_url = (payment_result.commercial_data or {}).get("cart_url")
    if not isinstance(cart_url, str):
        cart = (cart_result.commercial_data or {}).get("cart")
        cart_url = cart.get("cart_url") if isinstance(cart, dict) else None
    if payment_result.safety_reason and isinstance(cart_url, str):
        reply = f"{reply}\nSeu carrinho continua disponÃ­vel no checkout oficial:\n{cart_url}"
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        handoff_required=False,
        safety_reason=payment_result.safety_reason or cart_result.safety_reason,
        commercial_data=commercial_data,
        response_metadata=metadata,
    )


def _purchase_product_required_result(
    state: CommerceConversationState,
) -> AgentResult:
    ambiguous = bool(state.last_presented_products)
    return AgentResult(
        reply_text=(
            "Confirme qual produto vocÃª quer comprar antes de eu preparar o carrinho."
            if ambiguous
            else "Preciso saber qual produto vocÃª quer comprar antes de preparar o carrinho."
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason="product_ambiguous" if ambiguous else "no_cart_no_product",
        commercial_data={
            "products": [
                item.model_dump(mode="json")
                for item in state.last_presented_products[:3]
            ],
            "cart": {"status": "product_required"},
        },
    )


def _pending_product_references(
    state: CommerceConversationState,
) -> list[CommerceProductReference]:
    by_id: dict[str, CommerceProductReference] = {}
    if state.active_product is not None:
        by_id[state.active_product.product_id] = state.active_product
    for product in state.last_presented_products:
        by_id[product.product_id] = CommerceProductReference.model_validate(
            product.model_dump(exclude={"position"})
        )
    if state.pending_action_product_ids:
        return [
            by_id[product_id]
            for product_id in state.pending_action_product_ids
            if product_id in by_id
        ]
    return [state.active_product] if state.active_product is not None else []


def _pending_action_rejected_result(
    interpretation: SalesInterpretation,
    state: CommerceConversationState,
) -> AgentResult:
    print("[sales.pending_action]", {
        "action": state.pending_action,
        "has_product": bool(_pending_product_references(state)),
        "confirmation": interpretation.confirmation,
        "executed": False,
    })
    interpretation._clear_pending_action = True
    return _mark_sales_result(
        AgentResult(
            reply_text="Tudo bem. Não vou executar essa ação.",
            intent="commerce",
            handoff_required=False,
            response_metadata={"clear_pending_action": True},
        ),
        interpretation=interpretation,
        goal=interpretation.goal,
        response_source="deterministic_fallback",
        used_openai_responder=False,
        used_tray=False,
        fallback_reason="pending_action_rejected",
    )


async def handle_sales_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    semantic_plan: dict[str, Any] | SalesInterpretation | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> AgentResult | None:
    interpretation = semantic_plan if isinstance(semantic_plan, SalesInterpretation) else None
    state = commerce_state or CommerceConversationState()
    log_purchase_progress("interpretation", "start")
    if isinstance(semantic_plan, SalesInterpretation):
        plan = interpretation_to_plan(semantic_plan, message.text)
    elif semantic_plan and semantic_plan.get("domain") == "commerce":
        plan = semantic_plan
    else:
        plan = await plan_sales_request(message)
    if not plan:
        log_purchase_progress(
            "interpretation",
            "blocked",
            "sales_plan_missing",
        )
        return None
    log_purchase_progress("interpretation", "success")
    print("[sales.purchase.orchestrator]", {
        "has_purchase_action": bool(
            interpretation and interpretation.purchase_action
        ),
        "has_payment_action": bool(
            interpretation and interpretation.payment_action
        ),
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(
            interpretation.purchase_items
            if interpretation is not None
            else []
        ),
        "reference_type": (
            interpretation.reference_type
            if interpretation is not None
            else None
        ),
        "reference_position_present": bool(
            interpretation
            and interpretation.reference_position is not None
        ),
        "confirmation": (
            interpretation.confirmation
            if interpretation is not None
            else None
        ),
        "has_pending_action": bool(state.pending_action),
        "current_purchase_stage": state.purchase_stage,
    })
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "reject"
    ):
        return _pending_action_rejected_result(interpretation, state)
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "none"
    ):
        interpretation._clear_pending_action = True
    resolved_product = None
    resolved_by = "none"
    if interpretation is not None:
        log_purchase_progress("reference_resolution", "start")
        resolved_product, resolved_by = resolve_commerce_reference(interpretation, state)
        log_purchase_progress(
            "reference_resolution",
            "success" if resolved_product is not None else "blocked",
            None if resolved_product is not None else "reference_not_resolved",
        )
        print("[sales.reference]", {
            "type": interpretation.reference_type,
            "position": interpretation.reference_position,
            "resolved": resolved_product is not None,
            "resolved_by": resolved_by,
        })
    purchase_action = interpretation.purchase_action if interpretation is not None else None
    purchase_requests: list[CartItemRequest] = []
    unresolved_purchase_items = 0
    unresolved_candidates: list[dict[str, Any]] = []
    pending_link_requested = False
    if (
        interpretation is not None
        and state.pending_action
        and interpretation.confirmation == "confirm"
    ):
        pending_references = _pending_product_references(state)
        pending_action = state.pending_action
        interpretation._clear_pending_action = True
        if pending_action in {"create_cart", "confirm_purchase"}:
            purchase_action = "create_cart"
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
            elif pending_references:
                purchase_requests.extend(
                    CartItemRequest(
                        product_reference=reference,
                        quantity=interpretation.quantity or 1,
                        resolved_from="pending_action",
                        variant_preferences=interpretation.preferences.model_dump(
                            mode="json",
                            exclude_none=True,
                        ),
                    )
                    for reference in pending_references
                )
        elif pending_action == "show_images":
            interpretation = interpretation.model_copy(update={"image_request": True})
            interpretation._clear_pending_action = True
            plan = interpretation_to_plan(interpretation, message.text)
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "show_payment_options":
            interpretation = interpretation.model_copy(
                update={
                    "payment_action": "payment_options",
                    "purchase_action": (
                        "create_cart"
                        if not state.cart_session_id and len(pending_references) == 1
                        else interpretation.purchase_action
                    ),
                }
            )
            interpretation._clear_pending_action = True
            plan = interpretation_to_plan(interpretation, message.text)
            purchase_action = interpretation.purchase_action
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        elif pending_action == "send_product_link":
            pending_link_requested = True
            if len(pending_references) == 1:
                resolved_product = pending_references[0]
                resolved_by = "product_id"
        print("[sales.pending_action]", {
            "action": pending_action,
            "has_product": bool(pending_references),
            "confirmation": interpretation.confirmation,
            "executed": bool(
                resolved_product
                or purchase_requests
                or pending_action == "show_payment_options"
            ),
        })
    if interpretation is not None and interpretation.purchase_items:
        for item in interpretation.purchase_items:
            log_purchase_progress("reference_resolution", "start")
            reference, item_resolved_by = resolve_purchase_item_reference(item, state)
            log_purchase_progress(
                "reference_resolution",
                "success" if reference is not None else "blocked",
                None if reference is not None else "purchase_item_not_resolved",
            )
            if (
                reference is None
                and item.reference_type == "explicit_product"
                and item.explicit_product_name
            ):
                log_purchase_progress("product_resolution", "start")
                item_subject = interpretation.subject.model_copy(update={
                    "model": item.explicit_product_name,
                    "reference": None,
                    "ean": None,
                })
                item_interpretation = interpretation.model_copy(
                    deep=True,
                    update={
                        "goal": "find",
                        "subject": item_subject,
                        "purchase_action": None,
                        "purchase_items": [],
                        "quantity": None,
                        "needs_clarification": False,
                        "ready_for_retrieval": True,
                    },
                )
                lookup = await _execute_compiled_product_retrieval(item_interpretation)
                candidates = (
                    (lookup.commercial_data or {}).get("products")
                    if lookup is not None
                    else None
                )
                candidates = candidates if isinstance(candidates, list) else []
                if len(candidates) == 1 and isinstance(candidates[0], dict):
                    reference = product_reference_from_product(candidates[0])
                    item_resolved_by = "explicit_product"
                    log_purchase_progress("product_resolution", "success")
                elif candidates:
                    unresolved_candidates = [
                        candidate
                        for candidate in candidates[:3]
                        if isinstance(candidate, dict)
                    ]
                    log_purchase_progress(
                        "product_resolution",
                        "blocked",
                        "ambiguous_purchase_item",
                    )
                else:
                    log_purchase_progress(
                        "product_resolution",
                        (
                            "failed"
                            if lookup is not None and lookup.safety_reason
                            else "blocked"
                        ),
                        (
                            lookup.safety_reason
                            if lookup is not None and lookup.safety_reason
                            else "product_not_found"
                        ),
                    )
            if reference is None:
                unresolved_purchase_items += 1
                continue
            purchase_requests.append(CartItemRequest(
                product_reference=reference,
                quantity=item.quantity,
                position=item.reference_position,
                resolved_from=item_resolved_by,
                variant_preferences=interpretation.preferences.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            ))
        print("[sales.cart.items]", {
            "requested_count": len(interpretation.purchase_items),
            "resolved_count": len(purchase_requests),
        })
        purchase_action = purchase_action or "create_cart"
    if (
        interpretation is not None
        and purchase_action == "create_cart"
        and not purchase_requests
        and resolved_product is None
        and any((
            interpretation.subject.reference,
            interpretation.subject.ean,
            interpretation.subject.model,
        ))
    ):
        log_purchase_progress("product_resolution", "start")
        lookup = await _execute_compiled_product_retrieval(interpretation)
        lookup_products = (
            (lookup.commercial_data or {}).get("products")
            if lookup is not None
            else None
        )
        lookup_products = lookup_products if isinstance(lookup_products, list) else []
        if len(lookup_products) == 1 and isinstance(lookup_products[0], dict):
            resolved_product = product_reference_from_product(lookup_products[0])
            resolved_by = "product_id"
            log_purchase_progress("product_resolution", "success")
        elif lookup_products:
            unresolved_purchase_items = 1
            unresolved_candidates = [
                candidate
                for candidate in lookup_products[:3]
                if isinstance(candidate, dict)
            ]
            log_purchase_progress(
                "product_resolution",
                "blocked",
                "ambiguous_purchase_item",
            )
        elif lookup is not None:
            log_purchase_progress(
                "product_resolution",
                "failed" if lookup.safety_reason else "blocked",
                lookup.safety_reason or "product_not_found",
            )
            return _mark_sales_result(
                lookup,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if lookup.safety_reason in {
                        "tray_adapter_unavailable",
                        "product_match_failed",
                    }
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=bool(lookup.response_metadata.get("used_tray", True)),
                fallback_reason=lookup.safety_reason,
            )
    if interpretation is not None and interpretation.image_request:
        if resolved_product is None:
            return _mark_sales_result(
                AgentResult(
                    reply_text="Preciso saber qual produto você quer ver antes de consultar a imagem.",
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="product_context_missing",
                ),
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
            )
        media_result = await resolve_product_image(
            product_reference=resolved_product,
            execute=execute_tool,
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            media_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            media_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if media_result.safety_reason == "product_media_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=media_result.safety_reason,
        )
    if interpretation is not None and pending_link_requested:
        if resolved_product is None:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        link_result = await _execute_contextual_product_lookup(
            interpretation,
            resolved_product,
        )
        link_products = (link_result.commercial_data or {}).get("products")
        link_product = (
            link_products[0]
            if isinstance(link_products, list)
            and link_products
            and isinstance(link_products[0], dict)
            else {}
        )
        product_url = link_product.get("url")
        if isinstance(product_url, str) and product_url.startswith(("https://", "http://")):
            link_result.reply_text = f"Este é o link oficial do produto:\n{product_url}"
        elif link_result.safety_reason is None:
            link_result.reply_text = "A Tray não informou um link oficial para este produto."
        link_result.response_metadata["clear_pending_action"] = True
        final = await _sales_response_with_openai(
            message,
            plan,
            link_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            link_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if link_result.safety_reason == "tray_adapter_unavailable"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=True,
            fallback_reason=link_result.safety_reason,
        )
    if (
        purchase_action is None
        and interpretation is not None
        and interpretation.goal == "buy"
        and resolved_product is not None
    ):
        purchase_action = "create_cart"
    payment_requested = bool(interpretation and interpretation.payment_action)
    payment_preference = (
        interpretation.payment_method_preference
        if interpretation is not None
        else None
    )
    if (
        payment_requested
        and payment_preference is not None
        and resolved_product is None
        and state.active_product is not None
    ):
        resolved_product = state.active_product
        resolved_by = "product_id"
    if (
        payment_requested
        and payment_preference is not None
        and resolved_product is not None
    ):
        purchase_action = purchase_action or "create_cart"
    needs_cart = bool(
        payment_requested
        and not state.cart_session_id
        and (
            purchase_action in {"create_cart", "checkout_question", "show_cart_link"}
            or purchase_requests
        )
    )
    print("[sales.purchase.orchestrator.decision]", {
        "intent": plan.get("intent"),
        "purchase_action": purchase_action,
        "has_active_product": state.active_product is not None,
        "purchase_item_count": len(purchase_requests),
        "has_cart_session": bool(state.cart_session_id),
        "needs_cart": needs_cart,
        "payment_requested": payment_requested,
    })
    if (
        not payment_requested
        and purchase_action in {"checkout_question", "show_cart_link"}
        and not state.cart_session_id
    ):
        if unresolved_purchase_items or (
            not purchase_requests and resolved_product is None
        ):
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )
        _ensured_state, ensured_result = await _ensure_cart_for_purchase(
            interpretation=interpretation,
            state=state,
            purchase_requests=purchase_requests,
            resolved_product=resolved_product,
        )
        if ensured_result is not None:
            final = await _sales_response_with_openai(
                message,
                plan,
                ensured_result,
                interpretation,
            )
            if final:
                return final
            return _mark_sales_result(
                ensured_result,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source=(
                    "technical_fallback"
                    if ensured_result.safety_reason == "cart_technical_failure"
                    else "deterministic_fallback"
                ),
                used_openai_responder=False,
                used_tray=bool(ensured_result.response_metadata.get("used_tray", True)),
                fallback_reason=ensured_result.safety_reason,
            )
    if payment_requested:
        if unresolved_purchase_items:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_state = state
        cart_result: AgentResult | None = None
        if not state.cart_session_id and needs_cart:
            payment_state, cart_result = await _ensure_cart_for_purchase(
                interpretation=interpretation,
                state=state,
                purchase_requests=purchase_requests,
                resolved_product=resolved_product,
            )
            if cart_result is not None and not payment_state.cart_session_id:
                return _mark_sales_result(
                    cart_result,
                    interpretation=interpretation,
                    goal=plan.get("goal"),
                    response_source=(
                        "technical_fallback"
                        if cart_result.safety_reason == "cart_technical_failure"
                        else "deterministic_fallback"
                    ),
                    used_openai_responder=False,
                    used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
                    fallback_reason=cart_result.safety_reason,
                )
        if not payment_state.cart_session_id:
            missing = _purchase_product_required_result(state)
            return _mark_sales_result(
                missing,
                interpretation=interpretation,
                goal=plan.get("goal"),
                response_source="deterministic_fallback",
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=missing.safety_reason,
            )

        payment_result = await inspect_payment_options(
            state=payment_state,
            installment_count=interpretation.installment_count,
            payment_method_preference=payment_preference,
            execute=execute_tool,
        )
        combined_result = (
            _combine_cart_and_payment_results(cart_result, payment_result)
            if cart_result is not None
            else payment_result
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            combined_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            combined_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if payment_result.safety_reason == "payment_options_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(combined_result.response_metadata.get("used_tray")),
            fallback_reason=payment_result.safety_reason,
        )
    if purchase_action == "inspect_cart":
        cart_result = await inspect_current_cart(state=state, execute=execute_tool)
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray")),
            fallback_reason=cart_result.safety_reason,
        )
    if purchase_action in {"show_cart_link", "checkout_question"}:
        cart_result = current_cart_reply(
            state,
            checkout_question=purchase_action == "checkout_question",
        )
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="sales_responder_unavailable",
        )
    if purchase_action == "create_cart" and unresolved_purchase_items:
        log_purchase_progress(
            "product_resolution",
            "blocked",
            "purchase_item_unresolved",
        )
        return _mark_sales_result(
            AgentResult(
                reply_text="Encontrei mais de uma possibilidade. Confirme quais itens da lista devem entrar no carrinho.",
                intent="commerce",
                handoff_required=False,
                safety_reason="ambiguous_purchase_item",
                commercial_data={
                    "products": unresolved_candidates or [
                        item.model_dump(mode="json")
                        for item in state.last_presented_products
                    ],
                    "cart": {"status": "item_clarification_required"},
                },
                response_metadata={"presented_products": bool(unresolved_candidates)},
            ),
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="purchase_item_unresolved",
        )
    if purchase_action == "create_cart" and (purchase_requests or resolved_product is not None):
        if purchase_requests:
            cart_result = await create_cart_items_checkout(
                item_requests=purchase_requests,
                state=state,
                execute=execute_tool,
            )
        else:
            cart_result = await create_cart_checkout(
                interpretation=interpretation,
                product_reference=resolved_product,
                state=state,
                execute=execute_tool,
            )
        final = await _sales_response_with_openai(
            message,
            plan,
            cart_result,
            interpretation,
        )
        print("[sales.responder]", {
            "source": "openai" if final else "deterministic_fallback",
        })
        if final:
            return final
        return _mark_sales_result(
            cart_result,
            interpretation=interpretation,
            goal=plan.get("goal"),
            response_source=(
                "technical_fallback"
                if cart_result.safety_reason == "cart_technical_failure"
                else "deterministic_fallback"
            ),
            used_openai_responder=False,
            used_tray=bool(cart_result.response_metadata.get("used_tray", True)),
            fallback_reason=cart_result.safety_reason or "sales_responder_unavailable",
        )
    discovery_state = _discovery_state(interpretation, recent_turns) if interpretation else None
    if discovery_state and discovery_state["force_retrieval"] and plan.get("intent") == "clarification":
        plan = {**plan, "intent": "recommendation"}
    print("[sales.agent] planner", {
        "source": plan.get("_source", "fallback"),
        "action": plan.get("intent"),
        "has_query": bool(plan.get("query")),
        "has_brand": bool((plan.get("filters") or {}).get("brand")),
        "has_model": bool((plan.get("filters") or {}).get("model")),
    })
    if discovery_state:
        print("[sales.discovery]", {
            "clarification_count": discovery_state["clarification_count"],
            "clarification_budget_remaining": discovery_state["clarification_budget_remaining"],
            "enough_information_to_search": discovery_state["enough_information_to_search"],
            "ready_for_retrieval": discovery_state["ready_for_retrieval"],
            "stop_clarification": discovery_state["stop_clarification"],
            "known_preferences_count": discovery_state["known_preferences_count"],
        })
    vague_query = str(plan.get("query") or "").strip().lower() in {"", "alguma coisa", "algo", "qualquer coisa", "um produto", "uma coisa", "produto"}
    if interpretation and discovery_state and _needs_clarification_before_retrieval(interpretation, plan, discovery_state):
        return await generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    if interpretation and discovery_state and vague_query and not discovery_state["force_retrieval"]:
        return await generate_clarification_reply(
            message=message,
            interpretation=interpretation,
            recent_turns=recent_turns,
            discovery_state=discovery_state,
        )
    if plan.get("intent") == "clarification" or vague_query:
        result = AgentResult(
            reply_text=str(plan.get("clarification_question") or "Qual característica ou preferência é mais importante para você?"),
            intent="commerce",
            handoff_required=False,
            safety_reason="commerce_clarification",
        )
        return _mark_sales_result(
            result,
            interpretation=None,
            goal=plan.get("goal"),
            response_source="deterministic_fallback",
            used_openai_responder=False,
            used_tray=False,
        )

    action = {
        "product_search": "product_search",
        "purchase_intent": "product_search",
        "recommendation": "product_search",
        "product_comparison": "product_search",
        "price": "product_price",
        "inventory": "product_inventory",
        "coupon": "coupon_search",
    }.get(str(plan.get("intent")))
    if not action:
        return None

    if interpretation is not None and resolved_product is not None:
        tray_result = await _execute_contextual_product_lookup(
            interpretation,
            resolved_product,
        )
    elif interpretation is not None and action == "product_search":
        tray_result = await _execute_compiled_product_retrieval(interpretation)
    else:
        queries = [str(plan.get("query") or "").strip()]
        code_value = re.sub(r"^(?:ean|sku|ref(?:er[êe]ncia)?)\s+", "", queries[0], flags=re.IGNORECASE)
        code_query = bool(re.fullmatch(r"[A-Za-z0-9._/-]+", code_value)) and any(char.isdigit() for char in code_value)
        subject = plan.get("subject") or {}
        if action == "product_search" and not code_query:
            model = str(subject.get("model") or "").strip()
            brand = str(subject.get("brand") or "").strip()
            if model:
                queries.append(model)
            if brand:
                queries.append(brand)
        queries = list(dict.fromkeys(query for query in queries if query or action == "coupon_search"))
        tray_result = None
        last_raw_result = None
        for attempt, query in enumerate(queries[:3], start=1):
            attempt_plan = {**plan, "query": query, "subject": {**(plan.get("subject") or {}), "query": query}}
            print("[sales.agent] tray_request", {"capability": action, "attempt": attempt, "strategy": "initial" if attempt == 1 else "progressive"})
            raw_result = await handle_commerce_message(
                message,
                facts,
                customer_context,
                action=action,
                query=query,
            )
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
    if interpretation is not None:
        tray_result.response_metadata.update({
            "active_topic": interpretation.active_topic,
            "purchase_stage": interpretation.purchase_stage,
            "active_preferences": interpretation.preferences.model_dump(
                mode="json",
                exclude_none=True,
            ),
        })
        if resolved_product is not None:
            tray_result.response_metadata["active_product"] = resolved_product.model_dump(mode="json")
            if not state.cart_session_id:
                tray_result.response_metadata.setdefault("pending_action", "create_cart")
                tray_result.response_metadata.setdefault(
                    "pending_action_product_ids",
                    [resolved_product.product_id],
                )
                print("[sales.pending_action]", {
                    "action": "create_cart",
                    "has_product": True,
                    "confirmation": interpretation.confirmation,
                    "executed": False,
                })
        if interpretation.goal == "buy" and resolved_product is not None:
            tray_result.response_metadata["activate_first_product"] = True
    if (
        plan.get("intent") in {"purchase_intent", "recommendation", "clarification"}
        and tray_result.safety_reason == "product_not_found"
        and not (discovery_state and discovery_state["force_retrieval"])
    ):
        if interpretation:
            return await generate_clarification_reply(
                message=message,
                interpretation=interpretation,
                recent_turns=recent_turns,
                context_note="A busca atual não trouxe candidatos confiáveis; peça um critério diferente sem afirmar que o produto não existe.",
                used_tray=True,
                discovery_state=discovery_state,
            )
    final = await _sales_response_with_openai(message, plan, tray_result, interpretation)
    print("[sales.agent] responder", {"source": "openai" if final else "deterministic_fallback"})
    if final:
        return final
    technical_failure = tray_result.safety_reason in {
        "tray_adapter_unavailable",
        "product_match_failed",
    }
    response_source = "technical_fallback" if technical_failure else "deterministic_fallback"
    return _mark_sales_result(
        tray_result,
        interpretation=interpretation,
        goal=plan.get("goal"),
        response_source=response_source,
        used_openai_responder=False,
        used_tray=True,
        fallback_reason=(
            tray_result.safety_reason
            if response_source == "technical_fallback"
            else "sales_responder_unavailable"
        ),
    )
