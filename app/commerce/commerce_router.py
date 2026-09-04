from __future__ import annotations

import re
from typing import Any

from app.models import AgentResult, IncomingMessage
from app.tray.tray_tools import execute_tool


COMMERCE_UNAVAILABLE = "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."


_CURRENT_PRODUCT_BY_CONTEXT: dict[str, dict[str, Any]] = {}


def clear_commerce_memory() -> None:
    _CURRENT_PRODUCT_BY_CONTEXT.clear()


def _context_key(message: IncomingMessage) -> str | None:
    return message.conversation_id or message.sender_key or message.sender_phone


def _remember_product(message: IncomingMessage, product: dict[str, Any]) -> None:
    key = _context_key(message)
    product_id = product.get("id")
    if not key or not product_id:
        return
    _CURRENT_PRODUCT_BY_CONTEXT[key] = {
        field: product.get(field)
        for field in ("id", "name", "reference", "ean", "brand")
        if product.get(field) is not None
    }


def _remembered_product(message: IncomingMessage) -> dict[str, Any] | None:
    key = _context_key(message)
    return _CURRENT_PRODUCT_BY_CONTEXT.get(key) if key else None


def extract_product_query(text: str | None) -> str:
    value = " ".join((text or "").strip().split())
    value = re.sub(r"^e\s+", "", value, count=1, flags=re.IGNORECASE)
    prefixes = (
        r"gostaria\s+de\s+(?:comprar\s+)?", r"quero\s+(?:comprar\s+|adquirir\s+)?", r"procuro\s+", r"busco\s+",
        r"voc\u00eas\s+t\u00eam", r"voces\s+tem", r"voc\u00eas\s+tem",
        r"voc\u00eas\s+vendem", r"voces\s+vendem", r"tem\s+estoque\s+(?:de|do|da)",
        r"tem\s+estoque", r"qual\s+o\s+pre\u00e7o\s+(?:de|do|da)",
        r"qual\s+o\s+preco\s+(?:de|do|da)", r"qual\s+o\s+pre\u00e7o", r"qual\s+o\s+preco",
        r"quanto\s+custa", r"quanto\s+fica", r"vende", r"vendem", r"tem\s+(?:o|a|um|uma)", r"tem",
    )
    for prefix in prefixes:
        before_prefix = value
        value = re.sub(rf"^\s*{prefix}\s*", "", value, count=1, flags=re.IGNORECASE)
        if value != before_prefix:
            break
    value = value.strip(" ?!.,;:")
    value = re.sub(r"^(?:o|a|um|uma)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"^(?:qual|quais)(?:\s+(?:e|eh|é|são|sao|era|eram))?(?:\s+(?:o|os|a|as))?\s+",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    return value.strip(" ?!.,;:")


def _is_follow_up_without_product(query: str) -> bool:
    normalized = query.lower().strip()
    return not normalized or normalized in {
        "desse relogio", "desse relógio", "desse produto", "deste produto",
        "dele", "dela", "esse produto", "esse relogio", "esse relógio",
    } or normalized in {"estoque", "disponibilidade", "disponivel", "pix", "no pix", "e no pix", "quanto fica", "quanto fica no pix", "parcelamento", "parcelar", "promocao"}


def _fold_request_text(text: str | None) -> str:
    import unicodedata

    folded = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(text or "").casefold())
        if not unicodedata.combining(char)
    )
    return " ".join(folded.split())


def is_outbound_catalog_image_request(text: str | None) -> bool:
    """True when the customer wants official photos of already listed products."""
    normalized = _fold_request_text(text)
    if not normalized:
        return False
    inbound_photo = (
        "da foto",
        "na foto",
        "dessa foto",
        "nessa foto",
        "do relogio da foto",
        "da imagem que enviei",
        "da imagem que mandei",
    )
    if any(marker in normalized for marker in inbound_photo):
        return False
    has_photo = any(
        token in normalized
        for token in ("foto", "fotos", "imagem", "imagens")
    )
    if not has_photo:
        return False
    ask_to_send = any(
        token in normalized
        for token in (
            "manda",
            "envie",
            "envia",
            "mostra",
            "mostrar",
            "mandar",
            "quero ver",
            "pode mandar",
            "me manda",
            "me envia",
            "pedi a imagem",
            "pedi a foto",
            "que pedi",
            "cade a foto",
            "cade a imagem",
        )
    )
    return ask_to_send


def wants_all_listed_product_images(text: str | None) -> bool:
    normalized = _fold_request_text(text)
    return any(
        token in normalized
        for token in (
            "os tres",
            "os 3",
            "dos tres",
            "dos 3",
            "as tres",
            "as 3",
            "todos",
            "todas",
            "desses",
            "destes",
            "deles",
        )
    )


def is_listed_catalog_follow_up(text: str | None) -> bool:
    """Follow-up about the models just listed — not a new catalog search."""
    if is_outbound_catalog_image_request(text):
        return False
    normalized = _fold_request_text(text)
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "pronta entrega",
            "sob encomenda",
            "por encomenda",
            "prazo",
            "estoque",
            "disponivel",
            "todos",
            "todas",
            "desses",
            "destes",
            "deles",
            "os tres",
            "os 3",
        )
    )


def is_deictic_product_price_request(text: str | None) -> bool:
    """True for 'qual o preço desse/da foto' — must not reuse a stale SKU."""
    normalized = (text or "").casefold()
    if not normalized.strip():
        return False
    photo_markers = (
        "da foto",
        "na foto",
        "dessa foto",
        "nessa foto",
        "do relogio da foto",
        "do relógio da foto",
        "da imagem",
    )
    this_markers = (
        "desse",
        "dessa",
        "deste",
        "desta",
        "esse relogio",
        "esse relógio",
        "esse produto",
        "este relogio",
        "este relógio",
        "este produto",
    )
    return any(marker in normalized for marker in photo_markers + this_markers)


def resolve_commerce_action(text: str | None) -> str | None:
    normalized = (text or "").lower()
    if any(term in normalized for term in ("cupom comercial", "cupom disponível", "cupom disponivel", "algum cupom")):
        return "coupon_search"
    if any(term in normalized for term in ("estoque", "disponibilidade", "disponível", "disponivel")):
        return "product_inventory"
    if any(term in normalized for term in ("pix", "parcelamento", "parcelar", "promocao", "promoção")):
        return "product_price"
    if any(
        term in normalized
        for term in (
            "quanto custa",
            "qual o preço",
            "qual o preco",
            "preço",
            "preco",
            "valor",
        )
    ):
        return "product_price"
    if any(term in normalized for term in ("tem ", "vocês têm", "voces tem", "vende", "produto", "relógio", "relogio", "marca", "modelo", "sku", "ean")):
        return "product_search"
    return None


def _log_route(action: str, tool: str, has_query: bool) -> None:
    print("[commerce.route]", {"action": action, "tool": tool, "has_query": has_query})


def _products(result: dict[str, Any]) -> list[dict[str, Any]]:
    products = result.get("products") if isinstance(result, dict) else None
    return [item for item in products[:3] if isinstance(item, dict)] if isinstance(products, list) else []


def _as_money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept "3569.99" or "3.569,99"
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _price(product: dict[str, Any]) -> Any:
    for key in ("current_price", "promotional_price", "price"):
        if product.get(key) is not None:
            return product[key]
    return None


def _list_price(product: dict[str, Any]) -> float | None:
    """Shelf / a-prazo price (not the Pix cash amount)."""
    for key in ("current_price", "price"):
        money = _as_money(product.get(key))
        if money is not None and money > 0:
            return money
    return _as_money(product.get("promotional_price"))


def _price_label(value: Any) -> str | None:
    money = _as_money(value)
    if money is not None:
        return f"R$ {money:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if value is None:
        return None
    return str(value)


def _payment_details(product: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("payment_option_details", "payment_option"):
        value = product.get(key)
        if isinstance(value, dict):
            return value
    return None


def _pix_option_cash_value(pix: Any) -> float | None:
    if not isinstance(pix, dict):
        return None
    direct = _as_money(pix.get("value"))
    if direct is not None and direct > 0:
        return direct
    for key in ("application_value", "order_total", "total_base"):
        money = _as_money(pix.get(key))
        if money is not None and money > 0:
            # Prefer discounted application when present.
            if key == "total_base":
                discount = _as_money(pix.get("discount_value")) or 0.0
                if discount > 0:
                    return max(money - discount, 0.0)
            return money
    plots = pix.get("plots")
    if isinstance(plots, list):
        for plot in plots:
            if not isinstance(plot, dict):
                continue
            money = _as_money(plot.get("value") or plot.get("order_total"))
            if money is not None and money > 0:
                return money
    return None


def _pix_discount_percent() -> int:
    try:
        from app.persona.persona_runtime import DEFAULT_PIX_DISCOUNT_PERCENT, get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None:
            return int(
                getattr(runtime, "pix_discount_percent", None)
                or DEFAULT_PIX_DISCOUNT_PERCENT
            )
        return int(DEFAULT_PIX_DISCOUNT_PERCENT)
    except Exception as exc:  # noqa: BLE001
        from app.commerce import log_swallowed

        log_swallowed("router.pix_discount", exc)
        return 15


def _pix_cash_price(product: dict[str, Any], payment: dict[str, Any] | None) -> float | None:
    if payment:
        pix_value = _pix_option_cash_value(payment.get("pix"))
        if pix_value is not None:
            return pix_value
    list_price = _list_price(product)
    promo = _as_money(product.get("promotional_price"))
    if list_price is not None and promo is not None and 0 < promo < list_price:
        return promo
    if list_price is None:
        return promo
    percent = _pix_discount_percent()
    if percent <= 0 or percent >= 100:
        return None
    return round(list_price * (100 - percent) / 100.0, 2)


def _installment_lines(
    payment: dict[str, Any] | None,
    *,
    limit: int = 2,
) -> list[str]:
    if not payment:
        return []
    installments = payment.get("installments")
    if not isinstance(installments, list):
        return []
    parsed: list[tuple[bool, int, float]] = []
    for item in installments:
        if not isinstance(item, dict):
            continue
        count = item.get("count")
        amount = _as_money(item.get("value"))
        if not isinstance(count, int) or count < 2 or amount is None:
            continue
        parsed.append((bool(item.get("interest")), count, amount))
    if not parsed:
        return []
    # Prefer the best interest-free offer, then one with interest.
    no_interest = sorted(
        [row for row in parsed if not row[0]],
        key=lambda row: row[1],
        reverse=True,
    )
    with_interest = sorted(
        [row for row in parsed if row[0]],
        key=lambda row: row[1],
        reverse=True,
    )
    chosen: list[tuple[bool, int, float]] = []
    if no_interest:
        chosen.append(no_interest[0])
    if with_interest and len(chosen) < limit:
        chosen.append(with_interest[0])
    if not chosen:
        chosen = parsed[:limit]
    lines: list[str] = []
    for interest, count, amount in chosen[:limit]:
        interest_label = " com juros" if interest else " sem juros"
        amount_label = _price_label(amount)
        if amount_label:
            lines.append(f"{count}x de {amount_label}{interest_label}")
    return lines


def _payment_label(value: Any, *, compact: bool = False) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value[:120] if compact else value
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    pix_value = _pix_option_cash_value(value.get("pix"))
    if pix_value is not None:
        parts.append(f"Pix: {_price_label(pix_value)}")
    installments = value.get("installments")
    if isinstance(installments, list):
        limit = 1 if compact else 3
        for item in installments[:limit]:
            if not isinstance(item, dict):
                continue
            count = item.get("count") or "?"
            amount = _price_label(item.get("value"))
            interest = " com juros" if item.get("interest") else " sem juros"
            parts.append(f"{count}x{interest}" + (f" de {amount}" if amount else ""))
    return ", ".join(parts) or None


def _product_lines(
    products: list[dict[str, Any]],
    inventory: dict[str, Any] | None = None,
    *,
    compact: bool = False,
) -> list[str]:
    lines: list[str] = []
    for product in products:
        name = product.get("name") or "Produto encontrado"
        payment = _payment_details(product)
        list_price = _list_price(product)
        pix_price = _pix_cash_price(product, payment)
        product_url = (
            product.get("url")
            or product.get("product_url")
            or product.get("link")
        )
        if compact:
            block: list[str] = [str(name)]
            if product.get("reference"):
                block.append(f"Ref.: {product['reference']}")
            if list_price is not None:
                block.append(f"A prazo: {_price_label(list_price)}")
            if pix_price is not None and (
                list_price is None or abs(pix_price - list_price) >= 0.01
            ):
                # Ground derived Pix in commercial_data for factual validation.
                product["pix_price"] = pix_price
                block.append(f"À vista no Pix: {_price_label(pix_price)}")
            for installment in _installment_lines(payment, limit=2):
                block.append(installment)
            if isinstance(product_url, str) and product_url.strip():
                block.append(f"Link: {product_url.strip()}")
            lines.append("\n".join(block))
            continue

        parts = [str(name)]
        if product.get("reference"):
            parts.append(f"Ref.: {product['reference']}")
        if list_price is not None:
            parts.append(f"A prazo: {_price_label(list_price)}")
        elif (legacy := _price(product)) is not None:
            parts.append(f"Preço: {_price_label(legacy)}")
        if pix_price is not None and (
            list_price is None or abs(pix_price - list_price) >= 0.01
        ):
            product["pix_price"] = pix_price
            parts.append(f"À vista no Pix: {_price_label(pix_price)}")
        if isinstance(product_url, str) and product_url.strip():
            parts.append(f"Link: {product_url.strip()}")
        payment_text = _payment_label(payment) or _payment_label(
            product.get("payment_option")
        )
        if payment_text:
            parts.append(f"Condições comerciais: {payment_text}")
        for installment in _installment_lines(payment, limit=3):
            # Avoid duplicating installment text already covered by payment_text.
            if installment.split(" de ")[0] not in (payment_text or ""):
                parts.append(installment)
        if inventory:
            if inventory.get("stock") is not None:
                parts.append(f"Estoque: {inventory['stock']}")
            if inventory.get("availability"):
                parts.append(f"Disponibilidade: {inventory['availability']}")
            for key, label in (
                ("available_for_purchase", "Disponível para compra"),
                ("upon_request", "Sob consulta"),
            ):
                if inventory.get(key) is not None:
                    parts.append(f"{label}: {inventory[key]}")
        elif product.get("stock") is not None:
            parts.append(f"Estoque: {product['stock']}")
        lines.append(" | ".join(parts))
    return lines


def _product_result(action: str, products: list[dict[str, Any]]) -> AgentResult:
    if not products:
        return AgentResult(reply_text="N\u00e3o encontrei esse produto no cat\u00e1logo agora.", intent="commerce", handoff_required=False, safety_reason="product_not_found")
    if action == "product_disambiguation":
        prefix = "Encontrei algumas possibilidades:"
    else:
        prefix = "Sim, encontrei:" if action != "product_price" else "Encontrei:"
    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(products, compact=True), start=1)
    ]
    suffix = "\n\nÉ algum desses?" if action == "product_disambiguation" else ""
    return AgentResult(
        reply_text=prefix + "\n\n" + "\n\n".join(numbered_lines) + suffix,
        intent="commerce",
        handoff_required=False,
        commercial_data={"products": products},
    )


def guided_near_match_result(
    products: list[dict[str, Any]],
    *,
    brand: str | None = None,
    limit: int = 3,
    safety_reason: str = "exact_product_ambiguous_brand",
) -> AgentResult:
    """Present 2–3 color/line-locked options instead of bare not_found or brand dump."""
    shortlist = [product for product in products if isinstance(product, dict)][: max(1, min(limit, 5))]
    if not shortlist:
        return AgentResult(
            reply_text="Não encontrei esse produto no catálogo agora.",
            intent="commerce",
            handoff_required=False,
            safety_reason="product_not_found",
        )
    from app.sales.tray_query_authority import (
        budget_miss_from_authorization,
        current_catalog_authorization,
        products_within_authorization_budget,
    )

    authorization = current_catalog_authorization()
    if authorization is not None and authorization.forbid_near_match:
        in_budget = products_within_authorization_budget(shortlist, authorization)
        if not in_budget:
            return budget_miss_from_authorization(authorization, products)
        return _product_result("product_search", in_budget)
    brand_label = (brand or "").strip()
    if brand_label:
        prefix = (
            f"Não fechei a combinação exata, mas estes {brand_label} "
            "mais próximos batem com o que você pediu:"
        )
    else:
        prefix = (
            "Não fechei a combinação exata, mas estas opções próximas "
            "batem com o que você pediu:"
        )
    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(shortlist, compact=True), start=1)
    ]
    return AgentResult(
        reply_text=prefix + "\n" + "\n".join(numbered_lines) + "\n\nÉ algum desses?",
        intent="commerce",
        handoff_required=False,
        safety_reason=safety_reason,
        commercial_data={
            "products": shortlist,
            "match_status": "ambiguous",
        },
        response_metadata={
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
            "guided_near_match": True,
        },
    )


async def handle_commerce_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    *,
    action: str | None = None,
    query: str | None = None,
) -> AgentResult | None:
    del customer_context
    action = action or resolve_commerce_action(message.text)
    if not action:
        return None

    query = query if query is not None else extract_product_query(message.text)
    if action == "coupon_search":
        _log_route(action, "list_coupons", bool(query))
        result = await execute_tool("list_coupons", {"limit": 3})
        if "error" in result:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        coupons = result.get("coupons") if isinstance(result.get("coupons"), list) else []
        if not coupons:
            return AgentResult(reply_text="N\u00e3o encontrei cupons comerciais dispon\u00edveis agora.", intent="commerce", handoff_required=False, safety_reason="coupon_not_found")
        lines = [f"{coupon.get('code') or 'Cupom'}: {coupon.get('description') or 'dispon\u00edvel para consulta'}" for coupon in coupons[:3] if isinstance(coupon, dict)]
        return AgentResult(reply_text="Encontrei estes cupons comerciais:\n" + "\n".join(lines), intent="commerce", handoff_required=False)

    remembered = _remembered_product(message)
    if _is_follow_up_without_product(query):
        if not remembered or not remembered.get("id"):
            return AgentResult(
                reply_text="Qual produto você quer consultar? Informe o nome, modelo ou referência.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_context_missing",
            )
        product_id = str(remembered["id"])
        if action == "product_inventory":
            _log_route(action, "check_inventory", False)
            inventory = await execute_tool("check_inventory", {"product_id": product_id})
            if "error" in inventory:
                return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
            return AgentResult(reply_text="Consulta de estoque:\n" + "\n".join(_product_lines([remembered], inventory)), intent="commerce", handoff_required=False, commercial_data={"products": [remembered], "inventory": inventory})
        _log_route(action, "get_product", False)
        current = await execute_tool("get_product", {"product_id": product_id})
        if "error" in current:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        identity = {key: remembered.get(key) for key in ("id", "name", "reference", "ean", "brand") if remembered.get(key) is not None}
        return _product_result(action, [{**identity, **current}])

    _log_route(action, "search_products", True)
    search = await execute_tool("search_products", {"query": query, "limit": 3})
    if "error" in search:
        return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
    products = _products(search)
    if action == "product_price" and len(products) == 1 and products[0].get("id"):
        _log_route(action, "get_product", True)
        current = await execute_tool("get_product", {"product_id": str(products[0]["id"])})
        if "error" in current:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        identity = {key: products[0].get(key) for key in ("id", "name", "reference", "ean", "brand") if products[0].get(key) is not None}
        detail = {**identity, **current}
        _remember_product(message, detail)
        return _product_result(action, [detail])
    if action != "product_inventory":
        if len(products) == 1:
            _remember_product(message, products[0])
        return _product_result(action, products)
    if not products:
        return _product_result(action, products)
    if len(products) != 1:
        return AgentResult(reply_text="Encontrei mais de um produto com esse termo. Pode informar a refer\u00eancia ou o modelo exato?", intent="commerce", handoff_required=False, safety_reason="ambiguous_product")

    product_id = products[0].get("id")
    if not product_id:
        return AgentResult(reply_text="N\u00e3o consegui identificar esse produto para confirmar o estoque.", intent="commerce", handoff_required=False, safety_reason="product_id_missing")
    _remember_product(message, products[0])
    _log_route(action, "check_inventory", True)
    inventory = await execute_tool("check_inventory", {"product_id": str(product_id)})
    if "error" in inventory:
        return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
    return AgentResult(reply_text="Consulta de estoque:\n" + "\n".join(_product_lines(products, inventory)), intent="commerce", handoff_required=False, commercial_data={"products": products, "inventory": inventory})
