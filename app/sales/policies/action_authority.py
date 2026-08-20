from __future__ import annotations

from typing import Any

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, SalesInterpretation

# Política comercial oficial da New Store (persona Crono).
PIX_DISCOUNT_PERCENT = 15


def is_informational_payment_query(
    interpretation: SalesInterpretation | None,
    *,
    purchase_action: str | None,
    has_purchase_requests: bool = False,
) -> bool:
    """Perguntas de PIX/desconto/formas de pagamento sem compromisso de checkout."""
    if interpretation is None:
        return False
    if purchase_action in {"create_cart", "checkout_question", "show_cart_link"}:
        return False
    if has_purchase_requests:
        return False
    if interpretation.payment_request_kind == "checkout":
        return False
    if interpretation.confirmation == "confirm":
        return False
    if interpretation.payment_request_kind == "informational":
        return True
    # Preferência ou pergunta de pagamento sem kind=checkout: resposta informativa.
    return bool(
        interpretation.payment_action
        or interpretation.payment_method_preference
    )


def informational_payment_policy_result(
    state: CommerceConversationState,
    *,
    payment_method_preference: str | None = None,
) -> AgentResult:
    """Responde política de pagamento sem exigir/criar carrinho."""
    products = [
        item.model_dump(mode="json")
        for item in state.last_presented_products[:3]
    ]
    preference = (payment_method_preference or "").strip().lower()
    if preference == "pix":
        reply_text = (
            f"No PIX o desconto oficial da New Store é de {PIX_DISCOUNT_PERCENT}% "
            "sobre o valor do site — não consigo aplicar mais do que isso. "
            "Se quiser, me diga qual modelo te interessa que eu te passo o valor no PIX."
        )
    else:
        reply_text = (
            f"Aceitamos PIX (com {PIX_DISCOUNT_PERCENT}% de desconto sobre o valor do site), "
            "cartão e boleto conforme as opções oficiais. "
            "O preço do site é final; desconto além dos 15% no PIX só com consultor humano. "
            "Quer que eu calcule em algum modelo específico?"
        )
    commercial_data: dict[str, Any] = {
        "payment_policy": {
            "pix_discount_percent": PIX_DISCOUNT_PERCENT,
            "max_pix_discount_percent": PIX_DISCOUNT_PERCENT,
            "site_price_is_final": True,
            "negotiation_beyond_pix": "human_handoff",
        },
        "products": products,
        "cart": {"status": "not_required_for_informational_payment"},
        "action_guard": {
            "action": "payment_options",
            "allowed": True,
            "blocking_reason": None,
            "cart_required": False,
        },
    }
    if preference:
        commercial_data["payment_method_preference"] = preference
    return AgentResult(
        reply_text=reply_text,
        intent="commerce",
        handoff_required=False,
        safety_reason="informational_payment_no_cart",
        commercial_data=commercial_data,
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "payment_discussion",
        },
    )


def purchase_product_required_result(
    state: CommerceConversationState,
) -> AgentResult:
    ambiguous = bool(state.last_presented_products)
    return AgentResult(
        reply_text=(
            "Qual desses produtos você quer? Assim eu te passo o valor e as formas de pagamento."
            if ambiguous
            else "Me diga qual produto você quer que eu te passo o valor e as formas de pagamento."
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
            "action_guard": {
                "action": "create_cart",
                "allowed": False,
                "blocking_reason": (
                    "product_selection_required"
                    if ambiguous
                    else "product_target_missing"
                ),
            },
        },
        response_metadata={"domain": "commerce"},
    )
