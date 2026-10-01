"""Keep a new commercial request separate from earlier order/budget state."""
import re
from uuid import uuid4

from app.catalog.retrieval.text import fold_text
from app.commerce.commerce_context import CommerceConversationState
from app.commerce.order_service import extract_order_reference, extract_valid_tax_document


def reset_for_new_request(state, text, conversation_id=None, inbound_id=None):
    from app.sales.dialogue_phase import is_fresh_commerce_start, is_new_commerce_thread

    value = fold_text(text)
    new_subject = bool(re.search(
        r"\b(?:novo|outro) pedido\b|\b(?:novo|outro) orcamento\b|"
        r"\b(?:nova|outra) (?:compra|cotacao)\b", value
    )) and not re.search(r"\bnao (?:quero |e |tenho )?(?:um )?(?:novo|outro|nova|outra)\b", value)
    reference = extract_order_reference(text)
    old_order = state.order_id or state.order_lookup_id
    different_order = bool(reference and old_order and reference != str(old_order))
    if not (new_subject or different_order or is_fresh_commerce_start(text)
            or is_new_commerce_thread(conversation_id, state)):
        return state, False
    # This only resets the conversation snapshot, not real orders/carts.
    return CommerceConversationState(
        last_conversation_id=conversation_id,
        history_cut_inbound_id=inbound_id or state.history_cut_inbound_id,
        commercial_context_id=str(inbound_id) if inbound_id is not None else uuid4().hex,
        forget_shortlist=True,
        context_repairs=["new_commercial_request"],
    ), True


def ambiguous_number_question(text, turns, handles):
    """A bare number needs an immediately preceding, unambiguous question."""
    if extract_valid_tax_document(text):
        return None
    if not re.fullmatch(r"\s*#?\d{3,12}\s*", str(text or "")):
        return None
    if handles.get("contextual_order_ids"):
        return None
    previous = next((t for t in reversed(turns or [])
                     if isinstance(t, dict) and str(t.get("content") or "").strip()), {})
    prompt = fold_text(previous.get("content") or "")
    budget_prompt = bool(re.search(
        r"\borcamento\b|\bfaixa de (?:preco|investimento|valor)\b|"
        r"\bquanto.{0,25}(?:gastar|investir)\b|\b(?:valor|limite) maximo\b", prompt
    ))
    if previous.get("role") == "assistant" and budget_prompt and "pedido" not in prompt:
        return None
    return "Esse número é o número do pedido ou o valor que você pretende gastar?"
