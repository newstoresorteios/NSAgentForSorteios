"""Keep service updates out of catalog discovery without changing buying policy."""
from __future__ import annotations

import re

from app.catalog.retrieval.text import fold_text
from app.models import AgentResult


def service_intent_clarification(text, interpretation, state, *, catalog_fallback=False):
    from app.ops.handoff_consent import customer_requests_human
    if customer_requests_human(text):
        return None
    strategy = getattr(interpretation, "resolved_answer_strategy", None)
    if callable(strategy) and strategy() == "handoff":
        return None
    value = fold_text(text)
    # Explicit new shopping intent continues through the existing qualification.
    shopping = bool(re.search(
        r"\b(?:quero|gostaria de|pretendo|vou) comprar\b|\bprocuro\b|\bprocurando\b", value
    ))
    update_request = bool(re.search(
        r"\b(?:alguma|tem|ha) (?:noticia|novidade|atualizacao|retorno)\b|"
        r"\b(?:noticias|novidades) (?:sobre|da|do)\b", value
    ))
    after_sales = interpretation is not None and (
        interpretation.goal == "after_sales" or interpretation.purchase_stage == "after_sales"
    )
    if shopping or not (update_request or after_sales):
        return None
    from app.commerce.order_service import extract_valid_tax_document
    if (getattr(interpretation, 'order_id', None) or extract_valid_tax_document(text)
            or re.search(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b', text or '')):
        # Let the existing read-only lookup validate ownership and fetch orders.
        return None
    known_order = bool(state and (state.order_id or state.order_lookup_id))
    # Existing order handlers must get their opportunity before this fallback.
    if known_order and not catalog_fallback:
        return None
    if known_order:
        from app.ops.handoff_service import build_human_handoff_result
        return build_human_handoff_result(reason="after_sales_unresolved")
    if update_request:
        reply = (
            "Você está aguardando um pedido ou um retorno de atendimento, "
            "ou está procurando um produto para comprar?"
        )
    else:
        reply = ("Sim, pode ser o CPF do titular ou o e-mail usado na compra para localizar o pedido."
                 if re.search(r'\bcpf\b|\be.?mail\b|nao (?:tenho|sei|lembro).{0,25}(?:numero|pedido)', value)
                 else "Você pode me informar o número do pedido? Se não tiver, pode ser o CPF do titular ou o e-mail usado na compra.")
    return AgentResult(
        reply_text=reply, intent="support", handoff_required=False,
        safety_reason="service_intent_clarification",
        response_metadata={"domain": "commerce", "goal": "after_sales",
                           "response_source": "service_intent_clarification",
                           "used_tray": False},
    )
