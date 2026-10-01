from __future__ import annotations

from app.configuration.runtime import message as operator_message

from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.models import AgentResult, SalesInterpretation
from app.persona.persona_runtime import (
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
    pix_pct = int(policy["pix_discount_percent"] if policy.get("pix_discount_percent") is not None else PIX_DISCOUNT_PERCENT)
    max_pix = int(policy["max_pix_discount_percent"] if policy.get("max_pix_discount_percent") is not None else pix_pct)
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
            operator_message('sales.policies.action_authority.informational_payment_policy_result.1e4776f73c', pix_pct=f'{pix_pct}')
        )
    else:
        reply_text = (
            operator_message('sales.policies.action_authority.informational_payment_policy_result.7c0782063f', pix_pct=f'{pix_pct}', max_pix=f'{max_pix}', beyond=f'{beyond}')
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


def try_informational_payment(message, interpretation, state):
    """Answer a policy question before shortlist repair can turn it into a buy.

    Cart-specific amounts, explicit mutations and mixed product lookups keep
    their existing routes. A remembered model is not itself a new lookup.
    """
    if interpretation is None or interpretation.domain != "commerce":
        return None
    if any((message.image_url, interpretation.purchase_action, interpretation.order_action,
            interpretation.shipping_action, interpretation.checkout_action,
            interpretation.checkout_data, interpretation.product_action,
            interpretation.image_request)):
        return None
    if not is_informational_payment_query(
        interpretation, purchase_action=interpretation.purchase_action,
        has_purchase_requests=bool(interpretation.purchase_items),
    ):
        return None
    if interpretation.resolved_answer_strategy() == "handoff":
        return None
    from app.catalog.retrieval.text import fold_text
    from app.ops.handoff_consent import customer_requests_human
    from app.sales.purchase_selection import is_checkout_utterance, is_bare_purchase_closing
    text = fold_text(message.text)
    if (customer_requests_human(message.text) or is_checkout_utterance(message.text)
            or is_bare_purchase_closing(message.text)):
        return None
    import re
    policy_question = bool(re.search(r"\b(?:desconto|primeira compra|formas? de pagamento|aceita[m]? pix)\b", text))
    # A total/installment simulation belongs to the live payment adapter.
    if (not policy_question or re.search(r"\b(?:quanto|valor|total|simul|calcule|calcular)\b", text)
            or "catalog" in interpretation.information_needed
            or interpretation.installment_count):
        return None
    from app.sales.result_utils import mark_sales_result
    result = informational_payment_policy_result(state,
        payment_method_preference=interpretation.payment_method_preference)
    if re.search(r"\b(?:desconto|primeira compra)\b", text):
        payment = result.commercial_data["payment_policy"]
        pct = payment["pix_discount_percent"]
        result.reply_text = (
            f"O desconto que consigo confirmar é o de {pct}% no PIX. "
            "Ele não deve ser somado novamente ao valor no PIX já exibido no site."
        )
        if "primeira compra" in text:
            result.reply_text += " Não tenho confirmação de um desconto adicional exclusivo para a primeira compra."
        if payment["negotiation_beyond_pix"] == "human_handoff":
            result.reply_text += " Uma condição adicional depende de aprovação da equipe."
    else:
        # Do not ask again for a model the customer already supplied.
        result.reply_text = result.reply_text.split(" Quer que eu calcule", 1)[0]
    result.commercial_data["products"] = []
    result.response_metadata["read_only_payment_policy"] = True
    result.response_metadata["payment_policy_question"] = message.text
    return mark_sales_result(result, interpretation=interpretation, goal=interpretation.goal,
        response_source="published_payment_policy", used_openai_responder=False, used_tray=False)


def verified_informational_payment_reply(result: AgentResult) -> bool:
    """Authorize the exact policy copy, never an arbitrary discount claim.

    Rebuild using this turn's runtime policy. Neither a boolean metadata flag
    nor an LLM-supplied percentage is sufficient evidence for a promotion.
    """
    meta = result.response_metadata or {}
    if not meta.get("read_only_payment_policy") or not meta.get("payment_policy_question"):
        return False
    from app.models import IncomingMessage
    try:
        interpretation = SalesInterpretation.model_validate(meta.get("interpretation"))
    except (ValueError, TypeError):
        return False
    expected = try_informational_payment(IncomingMessage(text=meta["payment_policy_question"]),
        interpretation, CommerceConversationState())
    if expected is None or (result.commercial_data or {}).get("products"):
        return False
    if (result.commercial_data or {}).get("payment_policy") != expected.commercial_data["payment_policy"]:
        return False
    from app.identity.agent_disclosure import apply_agent_disclosure
    from app.llm.response_composer import normalize_reply_text
    allowed = [expected.reply_text]
    for introduce in (False, True):
        allowed.append(apply_agent_disclosure(expected.model_copy(deep=True), introduce=introduce).reply_text)
    return normalize_reply_text(result.reply_text) in [normalize_reply_text(text) for text in allowed]


def purchase_product_required_result(
    state: CommerceConversationState,
) -> AgentResult:
    ambiguous = bool(state.last_presented_products)
    return AgentResult(
        reply_text=(
            operator_message('sales.policies.action_authority.purchase_product_required_result.de22e507ce')
            if ambiguous
            else operator_message('sales.policies.action_authority.purchase_product_required_result.78d26293ee')
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
