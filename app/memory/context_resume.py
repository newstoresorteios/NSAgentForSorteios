from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.commerce.commerce_context import CommerceConversationState
from app.identity.greeting_policy import is_soft_greeting
from app.models import AgentResult


_SHORT_AFFIRMATIONS = {
    "sim",
    "si",
    "yes",
    "ok",
    "okay",
    "certo",
    "beleza",
    "blz",
    "pode",
    "pode ser",
    "isso",
    "uhum",
    "uhu",
}


def _fold(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join(
        "".join(char for char in normalized if not unicodedata.combining(char)).split()
    )


def commerce_state_resumable_score(state: dict[str, Any] | CommerceConversationState | None) -> int:
    payload = (
        state.model_dump(mode="json")
        if isinstance(state, CommerceConversationState)
        else (state if isinstance(state, dict) else {})
    )
    score = 0
    if payload.get("order_id") or payload.get("order_lookup_id"):
        score += 100
    if payload.get("order_payment_url"):
        score += 40
    if payload.get("pending_action") == "awaiting_payment":
        score += 30
    if payload.get("cart_session_id"):
        score += 20
    if payload.get("pending_action"):
        score += 10
    if payload.get("active_product") or payload.get("last_presented_products"):
        score += 5
    return score


def has_resumable_commerce(
    state: CommerceConversationState | dict[str, Any] | None,
) -> bool:
    return commerce_state_resumable_score(state) >= 20


def _presented_product_ids(products: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(products, list):
        return ids
    for item in products:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        if product_id:
            ids.add(product_id)
    return ids


def _prefer_latest_presentation(merged: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    """Keep the newest numbered shortlist so \"1\"/\"2\"/\"3\" match what the customer saw."""
    latest_presented = latest.get("last_presented_products")
    if not isinstance(latest_presented, list) or not latest_presented:
        return merged

    merged["last_presented_products"] = latest_presented
    if latest.get("product_resolution_state"):
        merged["product_resolution_state"] = latest["product_resolution_state"]
    if latest.get("active_topic"):
        merged["active_topic"] = latest["active_topic"]

    # Stale active_product from an older cart must not shadow a fresh shortlist.
    active = merged.get("active_product")
    active_id = ""
    if isinstance(active, dict):
        active_id = str(active.get("product_id") or "").strip()
    presented_ids = _presented_product_ids(latest_presented)
    if active_id and presented_ids and active_id not in presented_ids:
        merged["active_product"] = None
    return merged


_ORDER_RECOVERY_KEYS = (
    "order_id",
    "order_status",
    "order_status_group",
    "order_session_id",
    "order_created_at",
    "order_lookup_id",
    "order_payment_method_id",
    "order_payment_method",
    "order_payment_type",
    "order_payment_url",
    "order_payment_status",
    "order_has_payment",
    "order_payment_date",
    "pending_action",
    "purchase_stage",
    "cart_session_id",
    "cart_url",
    "checkout_draft",
)


def _copy_missing_fields(base: dict[str, Any], donor: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        if base.get(key) in (None, "", [], {}) and donor.get(key) not in (
            None,
            "",
            [],
            {},
        ):
            base[key] = donor[key]
    if (
        base.get("pending_action") is None
        and donor.get("pending_action") == "awaiting_payment"
    ):
        base["pending_action"] = "awaiting_payment"
    return base


def _strip_browse_memory(payload: dict[str, Any]) -> dict[str, Any]:
    stripped = dict(payload)
    stripped["last_presented_products"] = []
    stripped["active_product"] = None
    stripped["active_topic"] = None
    stripped["forget_shortlist"] = True
    stripped["product_resolution_state"] = None
    prefs = stripped.get("active_preferences")
    if isinstance(prefs, dict):
        cleaned = dict(prefs)
        cleaned.pop("locked_identity", None)
        stripped["active_preferences"] = cleaned
    if stripped.get("dialogue_phase") == "shortlist":
        stripped["dialogue_phase"] = "discovery"
    return stripped


def merge_commerce_states(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the latest state, but recover order/payment facts wiped by a later turn."""
    base = dict(primary or {})
    donor = dict(fallback or {})
    if not donor:
        return base
    if base.get("forget_shortlist"):
        recovered = _copy_missing_fields(base, donor, _ORDER_RECOVERY_KEYS)
        return _strip_browse_memory(recovered)
    if commerce_state_resumable_score(base) >= commerce_state_resumable_score(donor):
        # Still recover order fields if a later greeting/cart turn wiped them.
        if not base.get("order_id") and donor.get("order_id"):
            _copy_missing_fields(
                base,
                donor,
                _ORDER_RECOVERY_KEYS + ("active_product",),
            )
        return base
    # Richer cart/order donor wins, but never discard the latest product shortlist.
    return _prefer_latest_presentation(dict(donor), base)


def is_short_affirmation(text: str | None) -> bool:
    folded = _fold(text).strip("!?.,")
    return folded in _SHORT_AFFIRMATIONS


def is_payment_link_request(text: str | None) -> bool:
    folded = _fold(text)
    signals = (
        "me da o pix",
        "me dá o pix",
        "me da o link",
        "me dá o link",
        "me manda o link",
        "me envia o link",
        "manda o pix",
        "manda o link",
        "envia o pix",
        "envia o link",
        "passa o link",
        "link de pagamento",
        "link do pagamento",
        "link para pagamento",
        "link pro pagamento",
        "link pra pagamento",
        "pix para pagamento",
        "quero o pix",
        "quero o link",
        "gerar o pix",
        "gera o pix",
        "codigo pix",
        "código pix",
    )
    return any(signal in folded for signal in signals)


_PRESENTED_CATALOG_QUESTION_RE = re.compile(
    r"^"
    r"(?:e\s+)?"
    r"(?:"
    r"(?:me\s+)?mostra(?:r)?"
    r"(?:\s+(?:os relogios|as opcoes|os modelos|a opcao|a lista|"
    r"de novo|novamente|eles|elas|ai|os|as))?"
    r"|"
    r"(?:qual|quais)(?:\s+(?:era|eram|e|eh|sao))?"
    r"(?:\s+(?:o|os|a|as))?"
    r"\s+"
    r"(?:relogio|relogios|modelo|modelos|opcao|opcoes|"
    r"desses|destes|deles|desse|dessa|desta|da lista|mesmo)"
    r")"
    r"(?:\s+por favor)?"
    r"$"
)


_NON_MODEL_QUERY_TOKENS = frozenset(
    {
        "qual",
        "quais",
        "quanto",
        "quantos",
        "onde",
        "como",
        "quando",
        "relogio",
        "relogios",
        "watch",
        "modelo",
        "modelos",
        "opcao",
        "opcoes",
        "lista",
        "esses",
        "essas",
        "desse",
        "dessa",
        "deste",
        "desta",
        "desses",
        "destes",
        "deles",
        "o",
        "a",
        "os",
        "as",
        "um",
        "uma",
        "me",
        "mostra",
        "mostrar",
        "mesmo",
        "ai",
    }
)


def is_non_model_query(text: str | None) -> bool:
    """True when leftover tokens are interrogatives/type words, not a SKU."""
    tokens = [token.strip("!?.,") for token in _fold(text).split()]
    tokens = [token for token in tokens if token]
    return bool(tokens) and all(token in _NON_MODEL_QUERY_TOKENS for token in tokens)


def scrub_catalog_question_interpretation(interpretation: Any, text: str | None) -> Any:
    """Stop fallback from treating 'Qual relógio?' as model=Qual and searching Tray."""
    if interpretation is None:
        return interpretation
    subject = getattr(interpretation, "subject", None)
    if is_non_model_query(getattr(subject, "model", None) if subject is not None else None):
        subject.model = None
    if not is_presented_catalog_question(text):
        return interpretation
    interpretation.references_previous_context = True
    interpretation.enough_information_to_search = False
    interpretation.ready_for_retrieval = False
    interpretation.stop_clarification = False
    return interpretation


def is_presented_catalog_question(text: str | None) -> bool:
    """Short ask about the current shortlist — not a new brand/model search."""
    folded = _fold(text).strip("!?.,")
    if not folded:
        return False
    if re.match(
        r"^\s*(nenhuma|nenhum|nenhuma dessas|nenhum desses)\s*$",
        folded,
    ):
        return False
    return bool(_PRESENTED_CATALOG_QUESTION_RE.match(folded))


def should_redisplay_presented_catalog(
    text: str | None,
    state: CommerceConversationState,
) -> bool:
    """Replay last_presented_products instead of searching Tray for 'Qual'."""
    if not state.last_presented_products:
        return False
    return is_presented_catalog_question(text)


def is_generic_buy_continue(text: str | None) -> bool:
    """Buy/close phrasing that is not an explicit new-browse request."""
    folded = _fold(text)
    if not folded:
        return False
    signals = (
        "quero comprar",
        "vamos fechar",
        "fechar a compra",
        "fechar compra",
        "finalizar compra",
        "finalizar o pedido",
        "continuar a compra",
        "continuar compra",
        "continuar o pedido",
        "fechar pedido",
        "comprar um relogio",
        "comprar relogio",
    )
    return any(signal in folded for signal in signals)


def session_has_unpaid_order(state: CommerceConversationState) -> bool:
    status = str(state.order_payment_status or "").casefold()
    if status in {"paid", "confirmed", "approved"}:
        return False
    has_handle = bool(
        state.order_id
        or state.order_lookup_id
        or state.order_payment_url
    )
    if not has_handle:
        return False
    return (
        state.pending_action == "awaiting_payment"
        or state.purchase_stage == "awaiting_payment"
        or status in {"pending", "unpaid", "awaiting"}
    )


def is_unpaid_order_resume_request(text: str | None) -> bool:
    folded = _fold(text)
    if is_payment_link_request(text):
        return True
    unpaid_signals = (
        "nao paguei",
        "não paguei",
        "nao fiz o pagamento",
        "não fiz o pagamento",
        "falta pagar",
        "ainda nao paguei",
        "ainda não paguei",
        "link de pagamento",
        "link do pagamento",
        "quero pagar",
        "vou pagar",
        "pagamento caiu",
        "pagamento pendente",
        "confirmar se o pagamento",
        "confirma se o pagamento",
        "pagamento",
    )
    conversation_signals = (
        "acabamos de conversar",
        "acabamos de falar",
        "acabou de conversar",
        "ja conversamos",
        "já conversamos",
        "continuamos",
        "continuar",
        "mais acima",
    )
    return any(signal in folded for signal in unpaid_signals) or (
        "pedido" in folded and any(signal in folded for signal in conversation_signals)
    )


def should_resume_pending_order(
    text: str | None,
    state: CommerceConversationState,
    *,
    is_greeting: bool = False,
    allow_without_state: bool = False,
) -> bool:
    """Resume unpaid checkout instead of restarting discovery."""
    has_order_handle = bool(
        state.order_id
        or state.order_lookup_id
        or state.order_payment_url
        or (state.cart_session_id and state.pending_action == "awaiting_payment")
    )
    if not has_order_handle and not allow_without_state:
        return False
    folded = _fold(text)
    if re.match(
        r"^\s*(nenhuma|nenhum|nenhuma dessas|nenhum desses)\s*[!.?]*\s*$",
        folded,
    ):
        return False
    unpaid = session_has_unpaid_order(state) or (
        state.pending_action == "awaiting_payment" and has_order_handle
    )
    greeting = is_greeting or is_soft_greeting(text)
    if unpaid and is_presented_catalog_question(text):
        # Shortlist present → redisplay those models; otherwise dump PIX.
        return not bool(state.last_presented_products)
    if unpaid and (greeting or is_generic_buy_continue(text)):
        return True
    if greeting:
        return False
    if is_payment_link_request(text) or is_unpaid_order_resume_request(text):
        return True
    if unpaid and is_short_affirmation(text):
        return True
    return False


def build_presented_catalog_resume_result(
    state: CommerceConversationState,
) -> AgentResult | None:
    """Replay the numbered shortlist from memory — no Tray search."""
    items = list(state.last_presented_products[:3])
    if not items:
        return None
    products: list[dict[str, Any]] = []
    numbered: list[str] = []
    for position, item in enumerate(items, start=1):
        payload = {
            "id": item.product_id,
            "product_id": item.product_id,
            "name": item.name,
            "brand": item.brand,
            "reference": item.reference,
            "product_url": item.product_url,
            "url": item.product_url,
        }
        products.append(payload)
        parts = [item.name or f"opção {position}"]
        if item.reference:
            parts.append(f"Ref.: {item.reference}")
        if item.product_url:
            parts.append(f"Link: {item.product_url}")
        numbered.append(f"{position}. " + "\n".join(parts))
    metadata: dict[str, Any] = {
        "domain": "commerce",
        "presented_products": True,
        "product_resolution_state": state.product_resolution_state
        or "options_presented",
        "response_source": "context_resume_presented_catalog",
        "used_tray": False,
    }
    if state.pending_action == "awaiting_payment" or session_has_unpaid_order(state):
        metadata["pending_action"] = "awaiting_payment"
        if state.purchase_stage:
            metadata["purchase_stage"] = state.purchase_stage
        if state.order_id or state.order_lookup_id:
            metadata["order_state"] = {
                "order_id": state.order_id or state.order_lookup_id
            }
    return AgentResult(
        reply_text=(
            "Estes são os modelos que te mostrei:\n\n"
            + "\n\n".join(numbered)
        ),
        intent="commerce",
        commercial_data={"products": products},
        response_metadata=metadata,
    )


def build_pending_payment_resume_result(
    state: CommerceConversationState,
) -> AgentResult | None:
    url = str(state.order_payment_url or "").strip()
    if not url:
        return None
    order_label = state.order_id or state.order_lookup_id
    reply = (
        f"Seu pedido {order_label} ainda está aguardando pagamento. "
        f"Segue o link: {url}"
        if order_label
        else (
            "Seu pedido ainda está aguardando pagamento. "
            f"Segue o link: {url}"
        )
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        commercial_data={
            "order_id": order_label,
            "payment": {
                "payment_url": url,
                "status": state.order_payment_status or "awaiting_payment",
            },
        },
        response_metadata={
            "domain": "commerce",
            "pending_action": "awaiting_payment",
            "response_source": "context_resume_payment_url",
            "order_state": {"order_id": order_label} if order_label else {},
            "payment_state": {
                "order_payment_url": url,
                "order_payment_status": state.order_payment_status or "awaiting_payment",
            },
            "used_tray": False,
        },
    )


def build_contextual_greeting(
    state: CommerceConversationState,
    recent_turns: list[dict[str, Any]] | None = None,
) -> AgentResult:
    """Soft greeting: keep commerce memory silently; never volunteer order/payment."""
    from app.identity.greeting_policy import choose_greeting_reply

    _ = state  # Memory stays in pipeline state; reply remains non-intrusive.
    return AgentResult(
        reply_text=choose_greeting_reply(recent_turns),
        intent="general",
        response_metadata={
            "domain": "greeting",
            "response_source": "context_resume_soft",
        },
    )
