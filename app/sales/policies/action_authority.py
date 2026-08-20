from __future__ import annotations

from typing import Any

from app.commerce_context import CommerceConversationState
from app.models import AgentResult, SalesInterpretation
from app.persona_runtime import (
    DEFAULT_PIX_DISCOUNT_PERCENT,
    get_persona_runtime,
)

# Fallback when persona runtime is not loaded for the turn.
PIX_DISCOUNT_PERCENT = DEFAULT_PIX_DISCOUNT_PERCENT


def _payment_policy_from_runtime() -> dict[str, Any]:
    runtime = get_persona_runtime()
    if runtime is None:
        return {
            "pix_discount_percent": PIX_DISCOUNT_PERCENT,
            "max_pix_discount_percent": PIX_DISCOUNT_PERCENT,
            "site_price_is_final": True,
            "negotiation_beyond_pix": "human_handoff",
            "require_cart_for_informational_payment": False,
            "require_product_before_checkout": True,
            "policy_source": "defaults",
        }
    return runtime.flow_params_dict()


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
    policy = _payment_policy_from_runtime()
    pix_pct = int(policy.get("pix_discount_percent") or PIX_DISCOUNT_PERCENT)
    max_pix = int(policy.get("max_pix_discount_percent") or pix_pct)
    negotiation = str(policy.get("negotiation_beyond_pix") or "human_handoff")
    products = [
        item.model_dump(mode="json")
        for item in state.last_presented_products[:3]
    ]
    preference = (payment_method_preference or "").strip().lower()
    beyond = (
        "só com consultor humano"
        if negotiation == "human_handoff"
        else "não é possível"
    )
    if preference == "pix":
        reply_text = (
            f"No PIX o desconto oficial da New Store é de {pix_pct}% "
            "sobre o valor do site — não consigo aplicar mais do que isso. "
            "Se quiser, me diga qual modelo te interessa que eu te passo o valor no PIX."
        )
    else:
        reply_text = (
            f"Aceitamos PIX (com {pix_pct}% de desconto sobre o valor do site), "
            "cartão e boleto conforme as opções oficiais. "
            f"O preço do site é final; desconto além dos {max_pix}% no PIX {beyond}. "
            "Quer que eu calcule em algum modelo específico?"
        )
    commercial_data: dict[str, Any] = {
        "payment_policy": {
            "pix_discount_percent": pix_pct,
            "max_pix_discount_percent": max_pix,
            "site_price_is_final": bool(policy.get("site_price_is_final", True)),
            "negotiation_beyond_pix": negotiation,
            "policy_source": policy.get("policy_source"),
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
            "persona_runtime": {
                "persona_version_id": policy.get("persona_version_id"),
                "policy_source": policy.get("policy_source"),
            },
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
