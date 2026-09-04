"""Deterministic purchase selection against the last presented shortlist.

João (5548999490859, 31/08): "Quero comprar o 2" re-listed Baltic options instead
of creating a cart for list position 2. Keep this path off the LLM.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.commerce.commerce_context import CommerceConversationState
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
    r"pode\s+fechar|fechamos|fechar\s+pedido|comprar|"
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
    presented = list(state.last_presented_products or [])
    if not presented:
        return interpretation
    if interpretation.domain != "commerce":
        return interpretation

    position = parse_list_position_selection(message_text)
    bare_close = is_bare_purchase_closing(message_text)
    if not position and bare_close:
        position = _position_from_recent_turns(recent_turns)
        if position is None and len(presented) == 1:
            position = presented[0].position or 1
        if position is None and state.active_product is not None:
            # Bare "quero comprar" with an active SKU — buy that one.
            updates = {
                "goal": "buy",
                "purchase_action": "create_cart",
                "purchase_stage": "selection",
                "reference_type": "current_product",
                "reference_position": None,
                "needs_clarification": False,
                "clarification_question": None,
                "enough_information_to_search": True,
                "ready_for_retrieval": False,
                "stop_clarification": True,
                "confirmation": "confirm",
            }
            repaired = interpretation.model_copy(update=updates)
            print(
                "[sales.purchase.selection_repair]",
                {
                    "mode": "active_product",
                    "product_id": state.active_product.product_id,
                },
            )
            return repaired

    if position is None and not bare_close:
        return interpretation

    if position is not None:
        max_pos = max(int(item.position or 0) for item in presented)
        if position < 1 or position > max(max_pos, len(presented)):
            return interpretation
        updates = {
            "goal": "buy",
            "purchase_action": "create_cart",
            "purchase_stage": "selection",
            "reference_type": "list_position",
            "reference_position": position,
            "needs_clarification": False,
            "clarification_question": None,
            "enough_information_to_search": True,
            "ready_for_retrieval": False,
            "stop_clarification": True,
            "confirmation": "confirm",
        }
        repaired = interpretation.model_copy(update=updates)
        print(
            "[sales.purchase.selection_repair]",
            {
                "mode": "list_position",
                "position": position,
                "presented_count": len(presented),
            },
        )
        return repaired

    # Bare purchase with multi-item shortlist and no prior position: stay in buy
    # mode and ask which option — never reopen persona discovery.
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
        {
            "mode": "ask_which_option",
            "presented_count": len(presented),
        },
    )
    return repaired


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
