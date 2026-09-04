"""Deterministic purchase selection against the last presented shortlist.

"Quero comprar o 2" must bind list position 2 to a cart, not re-list options.
Keep this path off the LLM.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.commerce.commerce_context import (
    CommerceConversationState,
    PresentedCommerceProduct,
)
from ..models import SalesInterpretation

_ORDINAL_POSITIONS = {
    "primeiro": 1,
    "primeira": 1,
    "1o": 1,
    "1a": 1,
    "segundo": 2,
    "segunda": 2,
    "2o": 2,
    "2a": 2,
    "terceiro": 3,
    "terceira": 3,
    "3o": 3,
    "3a": 3,
}

_POSITION_RE = re.compile(
    r"\b(?:"
    r"(?:quero\s+)?(?:comprar|levar|pegar|ficar\s+com|quero)\s+(?:o|a|op(?:ç|c)(?:ã|a)o|opcao|n[uú]mero|num\.?|#)?\s*"
    r"|op(?:ç|c)(?:ã|a)o\s+|n[uú]mero\s+|#\s*"
    r")?(?P<num>[1-5])\b"
    r"|\b(?P<ord>primeiro|primeira|segundo|segunda|terceiro|terceira|1o|1a|2o|2a|3o|3a)\b",
    re.IGNORECASE,
)

_BARE_PURCHASE_RE = re.compile(
    r"^\s*("
    r"quero\s+comprar|quero\s+fechar|quero\s+levar|vou\s+levar|"
    r"pode\s+fechar|fechamos|fechar\s+pedido|fechar\s+a\s+compra|"
    r"comprar|"
    r"quero\s+esse|quero\s+este|quero\s+essa|quero\s+esta|"
    r"pode\s+ser|fechado|bora\s+fechar"
    r")\s*[!.?]*\s*$",
    re.IGNORECASE,
)

_CHECKOUT_UTTERANCE_RE = re.compile(
    r"\b("
    r"quero\s+(fechar|levar(\s+esse|\s+este|\s+essa|\s+esta)?)\b|"
    r"vou\s+levar|"
    r"pode\s+(fechar|montar\s+o\s+pedido)|"
    r"fechar\s+(o\s+)?pedido|"
    r"fechar\s+(a\s+)?(compra|negocio)|"
    r"como\s+(posso|podes|voce|voces|faz|fazer).{0,40}fechar|"
    r"pra\s+fechar|para\s+fechar|"
    r"fechamos|"
    r"bora\s+fechar|"
    r"montar\s+o\s+pedido|"
    r"gerar\s+(o\s+)?(pedido|carrinho)|"
    r"link\s+(de|do)\s+pagamento|"
    r"fechar\s+por\s+aqui|"
    r"continuar\s+pelo\s+site|"
    r"quero\s+(esse|este|essa|esta)\b"
    r")",
    re.IGNORECASE,
)

_NEW_BROWSE_RE = re.compile(
    r"\b("
    r"outras?\s+op(?:ç|c)(?:õ|o)es|outra\s+marca|outras?\s+marcas|"
    r"me\s+mostra|mostrar|ver\s+op(?:ç|c)(?:õ|o)es|sugest|"
    r"procuro|busco|quero\s+ver|quero\s+um\s+(?!d[eo]s?\b)|"
    r"gostaria\s+de\s+(?:ver|um|uma)"
    r")\b",
    re.IGNORECASE,
)

_GENERIC_NAME_TOKENS = frozenset(
    {
        "relogio",
        "relogios",
        "watch",
        "quero",
        "comprar",
        "levar",
        "pegar",
        "fechar",
        "pedido",
        "compra",
        "automatico",
        "automatica",
        "preto",
        "black",
    }
)

_SHORTLIST_WANT_RE = re.compile(
    r"\b(quero|vou\s+levar|comprar|fechar|pode\s+ser|leva)\b",
    re.IGNORECASE,
)


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def parse_list_position_selection(text: str | None) -> int | None:
    """Return 1-based list position when the customer picks from a shortlist."""
    raw = str(text or "").strip()
    if not raw:
        return None
    folded = _fold(raw)
    # Avoid treating "quero um relógio" / budget amounts as position picks.
    if _NEW_BROWSE_RE.search(folded) and not re.search(
        r"\b(comprar|levar|pegar|ficar com)\s+(o|a|opcao|opção|numero|número|#)?\s*[1-5]\b",
        folded,
    ):
        return None
    match = _POSITION_RE.search(folded)
    if not match:
        return None
    if match.group("num"):
        return int(match.group("num"))
    ordinal = match.group("ord")
    if ordinal:
        return _ORDINAL_POSITIONS.get(ordinal)
    return None


def is_bare_purchase_closing(text: str | None) -> bool:
    """True for short close-the-deal lines without a fresh catalog browse."""
    raw = str(text or "").strip()
    if not raw:
        return False
    if parse_list_position_selection(raw):
        return True
    if _NEW_BROWSE_RE.search(_fold(raw)):
        return False
    return bool(_BARE_PURCHASE_RE.match(raw))


def is_checkout_utterance(text: str | None) -> bool:
    """Customer asked to close, cart, or pay — not a generic 'quero um relógio'."""
    raw = str(text or "").strip()
    if not raw:
        return False
    folded = _fold(raw)
    if _NEW_BROWSE_RE.search(folded) and not _CHECKOUT_UTTERANCE_RE.search(folded):
        return False
    if is_bare_purchase_closing(raw):
        return True
    return bool(_CHECKOUT_UTTERANCE_RE.search(folded))


def _position_from_recent_turns(recent_turns: list[dict[str, Any]] | None) -> int | None:
    for turn in reversed(recent_turns or []):
        if turn.get("role") != "user":
            continue
        position = parse_list_position_selection(str(turn.get("content") or ""))
        if position:
            return position
    return None


def _distinctive_name_tokens(value: str | None) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]{3,}", _fold(value))
        if token not in _GENERIC_NAME_TOKENS
    ]


def _presented_from_state(
    state: CommerceConversationState,
) -> list[PresentedCommerceProduct]:
    presented = list(state.last_presented_products or [])
    if presented:
        return presented
    if state.cart_session_id:
        return []
    active = state.active_product
    if active is None:
        return []
    payload = active.model_dump()
    return [PresentedCommerceProduct(position=1, **payload)]


def match_presented_product_from_text(
    text: str | None,
    presented: list[PresentedCommerceProduct],
    *,
    active_product: Any = None,
) -> PresentedCommerceProduct | None:
    """Bind a unique ref/name, or the active SKU when only the brand is repeated."""
    folded = _fold(text)
    if not folded or not presented:
        return None
    scored: list[tuple[int, PresentedCommerceProduct]] = []
    for item in presented:
        score = 0
        ref = _fold(item.reference or "")
        if ref and len(ref) >= 4 and ref in folded:
            score += 20
        pid = _fold(item.product_id or "")
        if pid and len(pid) >= 4 and re.search(rf"\b{re.escape(pid)}\b", folded):
            score += 15
        brand = _fold(item.brand or "")
        distinctive = [
            token
            for token in _distinctive_name_tokens(item.name)
            if token != brand and (not brand or brand not in token)
        ]
        matched = [token for token in distinctive if token in folded]
        if distinctive and matched:
            score += 6 * len(matched)
        if brand and len(brand) >= 3 and re.search(rf"\b{re.escape(brand)}\b", folded):
            score += 2
        if score:
            scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], pair[1].position or 99))
    top = scored[0][0]
    winners = [item for score, item in scored if score == top]
    if len(winners) == 1 and top >= 6:
        return winners[0]
    if active_product is not None:
        active_id = str(active_product.product_id)
        for item in winners:
            if str(item.product_id) == active_id:
                return item
    if len(winners) == 1 and top >= 2 and sum(1 for score, _ in scored if score >= 2) == 1:
        return winners[0]
    return None


def _match_kind(text: str | None, item: PresentedCommerceProduct) -> str:
    folded = _fold(text)
    ref = _fold(item.reference or "")
    if ref and len(ref) >= 4 and ref in folded:
        return "ref"
    pid = _fold(item.product_id or "")
    if pid and len(pid) >= 4 and re.search(rf"\b{re.escape(pid)}\b", folded):
        return "ref"
    brand = _fold(item.brand or "")
    distinctive = [
        token
        for token in _distinctive_name_tokens(item.name)
        if token != brand and (not brand or brand not in token)
    ]
    if any(token in folded for token in distinctive):
        return "name"
    return "brand"


def _mentions_presented_catalog(
    text: str | None,
    presented: list[PresentedCommerceProduct],
) -> bool:
    folded = _fold(text)
    if not folded:
        return False
    for item in presented:
        brand = _fold(item.brand or "")
        if brand and len(brand) >= 3 and re.search(rf"\b{re.escape(brand)}\b", folded):
            return True
        ref = _fold(item.reference or "")
        if ref and len(ref) >= 4 and ref in folded:
            return True
        if any(
            len(token) >= 4 and token in folded
            for token in _distinctive_name_tokens(item.name)
        ):
            return True
    return False


def _create_cart_repair(
    interpretation: SalesInterpretation,
    *,
    mode: str,
    position: int | None,
    reference_type: str,
    extra: dict[str, Any] | None = None,
) -> SalesInterpretation:
    updates = {
        "goal": "buy",
        "purchase_action": "create_cart",
        "purchase_stage": "selection",
        "reference_type": reference_type,
        "reference_position": position,
        "needs_clarification": False,
        "clarification_question": None,
        "enough_information_to_search": True,
        "ready_for_retrieval": False,
        "stop_clarification": True,
        "confirmation": "confirm",
    }
    repaired = interpretation.model_copy(update=updates)
    print("[sales.purchase.selection_repair]", {"mode": mode, **(extra or {})})
    return repaired


def _ask_which_option_repair(
    interpretation: SalesInterpretation,
    presented: list[PresentedCommerceProduct],
) -> SalesInterpretation:
    labels = []
    for item in presented[:3]:
        name = (item.name or item.brand or item.product_id or "").strip()
        labels.append(f"{item.position}. {name}" if item.position else name)
    question = (
        "Qual opção você quer comprar?\n" + "\n".join(labels)
        if labels
        else "Qual das opções da lista você quer comprar (1, 2 ou 3)?"
    )
    repaired = interpretation.model_copy(
        update={
            "goal": "buy",
            "purchase_action": None,
            "purchase_stage": "selection",
            "needs_clarification": True,
            "clarification_question": question,
            "enough_information_to_search": True,
            "ready_for_retrieval": False,
            "stop_clarification": True,
            "active_topic": "purchase_option_choice",
        }
    )
    print(
        "[sales.purchase.selection_repair]",
        {"mode": "ask_which_option", "presented_count": len(presented)},
    )
    return repaired


def skips_discovery_clarification(
    interpretation: SalesInterpretation | None,
) -> bool:
    """Known close / inspect / cart must not reopen brand or name discovery."""
    if interpretation is None:
        return False
    if interpretation.image_request:
        return True
    if interpretation.purchase_action == "create_cart" and interpretation.stop_clarification:
        return True
    if interpretation.stop_clarification and interpretation.goal == "buy":
        if interpretation.active_topic == "purchase_option_choice":
            return False
        return True
    return False


def repair_presented_purchase_selection(
    interpretation: SalesInterpretation | None,
    *,
    message_text: str | None,
    state: CommerceConversationState | None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> SalesInterpretation | None:
    """Force list_position + create_cart when the shortlist is on screen."""
    if interpretation is None or state is None:
        return interpretation
    if interpretation.domain != "commerce":
        return interpretation
    if interpretation.image_request:
        return interpretation
    if (
        interpretation.purchase_action
        and interpretation.purchase_action != "create_cart"
    ):
        return interpretation
    presented = _presented_from_state(state)
    if not presented:
        return interpretation

    named = match_presented_product_from_text(
        message_text,
        presented,
        active_product=state.active_product,
    )
    closing = is_checkout_utterance(message_text) or is_bare_purchase_closing(
        message_text
    )
    browsing = bool(_NEW_BROWSE_RE.search(_fold(message_text)))
    wants_listed = bool(
        _SHORTLIST_WANT_RE.search(_fold(message_text)) and not browsing
    )
    if named is not None:
        kind = _match_kind(message_text, named)
        bind_named = not browsing and (
            kind == "ref"
            or (kind in {"name", "brand"} and (closing or wants_listed))
        )
        if bind_named:
            return _create_cart_repair(
                interpretation,
                mode=f"named_{kind}",
                position=named.position,
                reference_type="list_position" if named.position else "current_product",
                extra={
                    "product_id": named.product_id,
                    "position": named.position,
                    "kind": kind,
                },
            )

    position = parse_list_position_selection(message_text)
    mentions_listed = _mentions_presented_catalog(message_text, presented)
    if position is None and (closing or (wants_listed and mentions_listed)):
        position = _position_from_recent_turns(recent_turns)
        if position is None and len(presented) == 1:
            position = presented[0].position or 1
        if position is None and state.active_product is not None:
            return _create_cart_repair(
                interpretation,
                mode="active_product",
                position=None,
                reference_type="current_product",
                extra={"product_id": state.active_product.product_id},
            )
        if position is None:
            return _ask_which_option_repair(interpretation, presented)

    if position is None and not closing:
        return interpretation

    if position is not None:
        max_pos = max(int(item.position or 0) for item in presented)
        if position < 1 or position > max(max_pos, len(presented)):
            return interpretation
        return _create_cart_repair(
            interpretation,
            mode="list_position",
            position=position,
            reference_type="list_position",
            extra={"position": position, "presented_count": len(presented)},
        )

    return _ask_which_option_repair(interpretation, presented)


def blocks_persona_qualification_for_purchase(
    interpretation: SalesInterpretation | None,
    state: CommerceConversationState | None,
) -> bool:
    """Purchase closing / shortlist on screen must not reopen ChatBo discovery."""
    if interpretation is None or state is None:
        return False
    try:
        from .dialogue_phase import session_in_checkout_phase

        if session_in_checkout_phase(state):
            return False
    except Exception as exc:
        from app.sales import log_swallowed

        log_swallowed("purchase_selection.checkout_phase", exc)
    if state.last_presented_products or state.active_product is not None:
        return True
    if interpretation.goal == "buy":
        return True
    if interpretation.purchase_action == "create_cart":
        return True
    if interpretation.goal == "buy" and interpretation.stop_clarification:
        return True
    if interpretation.active_topic == "purchase_option_choice":
        return True
    return False
