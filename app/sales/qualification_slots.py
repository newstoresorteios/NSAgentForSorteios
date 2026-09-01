"""Deterministic persona qualification slot tracking (João loop / context wipe fix)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..models import SalesInterpretation
from ..history_window import turns_for_conversation

QUAL_PREFIX = "qual:"

CUSTOMER_NAME = "customer_name"
SHIPPING_CITY = "shipping_city"
URGENCY = "urgency"

_QUESTION_SLOT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (CUSTOMER_NAME, ("como posso te chamar", "chamar", "seu nome", "te chamar")),
    (SHIPPING_CITY, ("para qual cidade", "cidade seria", "cidade", "entrega")),
    (
        URGENCY,
        ("pressa para receber", "pode esperar", "sob encomenda", "pronta entrega"),
    ),
]

_NAME_RE = re.compile(r"^[A-Za-zÀ-ú][A-Za-zÀ-ú'\- ]{0,40}$")
_CITY_RE = re.compile(r"^[A-Za-zÀ-ú][A-Za-zÀ-ú'\- ]{1,60}$")
_BUDGET_ANSWER_RE = re.compile(
    r"\b(at[eé]|mil|r\$|reais|investimento|orçamento|orcamento|\d)\b",
    re.IGNORECASE,
)
_GENDER_LABELS = frozenset({"feminino", "masculino", "unissex", "unisex"})


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _is_clarification_turn(turn: dict[str, Any]) -> bool:
    metadata = turn.get("metadata") if isinstance(turn, dict) else None
    return (
        turn.get("role") == "assistant"
        and isinstance(metadata, dict)
        and metadata.get("safety_reason") == "commerce_clarification"
    )


def classify_qualification_question(text: str | None) -> str | None:
    """Map an assistant qualification prompt to a slot dimension."""
    folded = _fold(text)
    if not folded:
        return None
    for slot, needles in _QUESTION_SLOT_PATTERNS:
        if any(needle in folded for needle in needles):
            return slot
    return None


def last_assistant_qualification_slot(
    recent_turns: list[dict[str, Any]] | None,
    *,
    conversation_id: str | None = None,
) -> str | None:
    """Slot asked in the latest assistant turn of this thread, tagged or not."""
    for turn in reversed(turns_for_conversation(recent_turns, conversation_id)):
        if not isinstance(turn, dict) or turn.get("role") != "assistant":
            continue
        return classify_qualification_question(str(turn.get("content") or ""))
    return None


def is_qualification_slot_answer(
    recent_turns: list[dict[str, Any]] | None,
    message_text: str | None,
    *,
    conversation_id: str | None = None,
) -> bool:
    """True when the user is answering the last qualification question in this thread."""
    slot = last_assistant_qualification_slot(
        recent_turns, conversation_id=conversation_id
    )
    if not slot:
        return False
    text = " ".join(str(message_text or "").strip().split())
    if not text:
        return False
    if slot == CUSTOMER_NAME:
        return _is_plausible_name(text)
    if slot == SHIPPING_CITY:
        return _is_plausible_city(text)
    if slot == URGENCY:
        return len(text) <= 80
    return False


def continue_commerce_from_qualification_answer(
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None,
    message_text: str | None,
    *,
    conversation_id: str | None = None,
) -> SalesInterpretation:
    """Keep discovery open when a name/city/urgency answer is misread as greeting."""
    if not is_qualification_slot_answer(
        recent_turns, message_text, conversation_id=conversation_id
    ):
        return interpretation
    slot = last_assistant_qualification_slot(
        recent_turns, conversation_id=conversation_id
    )
    updated = apply_qualification_slot_answer(interpretation, slot, message_text)
    if updated.domain in {"greeting", "out_of_scope", "store_general", "general"}:
        subject = updated.subject
        if not str(subject.product_type or "").strip():
            subject = subject.model_copy(update={"product_type": "relógio"})
        updated = updated.model_copy(
            update={
                "domain": "commerce",
                "goal": updated.goal or "discover",
                "subject": subject,
                "references_previous_context": True,
                "needs_clarification": True,
            }
        )
    return updated


def _qual_attr_key(slot: str) -> str:
    short = {
        CUSTOMER_NAME: "name",
        SHIPPING_CITY: "city",
        URGENCY: "urgency",
    }.get(slot, slot)
    return f"{QUAL_PREFIX}{short}:"


def _get_qual_value(attributes: list[str], slot: str) -> str | None:
    prefix = _qual_attr_key(slot)
    for item in attributes:
        raw = str(item or "")
        if raw.startswith(prefix):
            value = raw[len(prefix) :].strip()
            return value or None
    return None


def _set_qual_value(attributes: list[str], slot: str, value: str) -> list[str]:
    prefix = _qual_attr_key(slot)
    kept = [item for item in attributes if not str(item).startswith(prefix)]
    kept.append(f"{prefix}{value}")
    return kept


def _normalize_urgency_answer(text: str) -> str:
    folded = _fold(text)
    if any(token in folded for token in ("pressa", "rapido", "rápido", "urgente", "hoje")):
        return "rush"
    if any(token in folded for token in ("esperar", "espero", "posso esperar", "sem pressa")):
        return "can_wait"
    return folded[:48] or "unknown"


_COMMERCE_NAME_BLOCK = frozenset(
    {
        "quero",
        "comprar",
        "relogio",
        "omega",
        "seiko",
        "tissot",
        "faixa",
        "preco",
        "orcamento",
        "investimento",
        "link",
        "pix",
        "ola",
        "oi",
    }
)


def _is_plausible_name(text: str) -> bool:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned or len(cleaned) > 48:
        return False
    folded = _fold(cleaned)
    if folded in _GENDER_LABELS:
        return False
    if _BUDGET_ANSWER_RE.search(cleaned):
        return False
    tokens = set(folded.split())
    if tokens & _COMMERCE_NAME_BLOCK:
        return False
    if any(token in folded for token in ("florian", "rio de", "são paulo", "curitiba")):
        return False
    return bool(_NAME_RE.match(cleaned))


def _is_plausible_city(text: str) -> bool:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned or len(cleaned) > 80:
        return False
    folded = _fold(cleaned)
    if folded in _GENDER_LABELS:
        return False
    if _BUDGET_ANSWER_RE.search(cleaned) and "mil" in folded:
        return False
    return bool(_CITY_RE.match(cleaned))


def covered_qualification_dims(interpretation: SalesInterpretation) -> set[str]:
    """Slots already answered in this thread (attributes + recipient when a name)."""
    prefs = interpretation.preferences
    attrs = list(prefs.attributes or [])
    covered: set[str] = set()

    if _get_qual_value(attrs, CUSTOMER_NAME):
        covered.add(CUSTOMER_NAME)
    elif prefs.recipient and _fold(prefs.recipient) not in _GENDER_LABELS:
        if _is_plausible_name(prefs.recipient):
            covered.add(CUSTOMER_NAME)

    if _get_qual_value(attrs, SHIPPING_CITY):
        covered.add(SHIPPING_CITY)

    urgency = _get_qual_value(attrs, URGENCY)
    if urgency:
        covered.add(URGENCY)
    else:
        blob = " ".join(str(item) for item in attrs).casefold()
        if any(
            token in blob
            for token in ("pronta", "urgência", "urgencia", "rápido", "rapido", "can_wait", "rush")
        ):
            covered.add(URGENCY)

    return covered


def apply_qualification_slot_answer(
    interpretation: SalesInterpretation,
    slot: str | None,
    answer_text: str | None,
) -> SalesInterpretation:
    """Persist a user answer to the matching qualification slot."""
    if not slot:
        return interpretation
    answer = " ".join(str(answer_text or "").strip().split())
    if not answer:
        return interpretation

    prefs = interpretation.preferences.model_copy(deep=True)
    attrs = list(prefs.attributes or [])

    if slot == CUSTOMER_NAME and _is_plausible_name(answer):
        prefs.recipient = answer
        attrs = _set_qual_value(attrs, CUSTOMER_NAME, answer)
    elif slot == SHIPPING_CITY and _is_plausible_city(answer):
        attrs = _set_qual_value(attrs, SHIPPING_CITY, answer)
    elif slot == URGENCY:
        attrs = _set_qual_value(attrs, URGENCY, _normalize_urgency_answer(answer))
    else:
        return interpretation

    prefs.attributes = attrs
    updates: dict[str, Any] = {"preferences": prefs}
    if slot == URGENCY or slot == SHIPPING_CITY:
        updates["stop_clarification"] = interpretation.stop_clarification
    return interpretation.model_copy(update=updates)


def rehydrate_qualification_slots_from_turns(
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None,
    *,
    message_text: str | None = None,
    conversation_id: str | None = None,
) -> SalesInterpretation:
    """Replay clarification Q→A pairs so slots survive across turns in this thread."""
    updated = interpretation
    turns = turns_for_conversation(recent_turns, conversation_id)
    pending_question: str | None = None
    for turn in turns:
        if turn.get("role") == "assistant":
            content = str(turn.get("content") or "").strip() or None
            if _is_clarification_turn(turn) or classify_qualification_question(content):
                pending_question = content
            continue
        if turn.get("role") != "user":
            continue
        if not pending_question:
            continue
        slot = classify_qualification_question(pending_question)
        if slot:
            updated = apply_qualification_slot_answer(
                updated,
                slot,
                str(turn.get("content") or ""),
            )
        pending_question = None

    # Current turn: last assistant qualification prompt + this user message.
    last_q: str | None = None
    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "").strip() or None
        if _is_clarification_turn(turn) or classify_qualification_question(content):
            last_q = content
            break
    if last_q and message_text:
        slot = classify_qualification_question(last_q)
        if slot:
            updated = apply_qualification_slot_answer(updated, slot, message_text)

    return updated


def _has_explicit_model(interpretation: SalesInterpretation) -> bool:
    model = str(interpretation.subject.model or "").strip()
    if not model:
        return False
    brand_fold = _fold(interpretation.subject.brand)
    model_fold = _fold(model)
    if brand_fold and model_fold == brand_fold:
        return False
    leftover = model_fold
    for token in ("relogio", "relógio", "watch"):
        leftover = leftover.replace(token, " ")
    return bool(" ".join(leftover.split()))


def qualification_slots_sufficient(
    interpretation: SalesInterpretation,
    covered: set[str],
) -> bool:
    """Enough persona slots collected — stop re-qualifying from question 1."""
    has_name = CUSTOMER_NAME in covered
    has_city = SHIPPING_CITY in covered
    has_brand = bool(interpretation.subject.brand)
    has_model = _has_explicit_model(interpretation) or bool(interpretation.subject.reference)
    has_budget = interpretation.preferences.budget_max is not None or interpretation.preferences.budget_min is not None or "budget" in covered
    has_urgency = URGENCY in covered
    return bool(has_name and has_city and has_brand and has_model and has_budget and has_urgency)


def known_preferences_from_qualification_slots(
    interpretation: SalesInterpretation,
) -> dict[str, Any]:
    """Expose qual slots in discovery known_preferences / covered_dims."""
    prefs = interpretation.preferences
    attrs = list(prefs.attributes or [])
    known: dict[str, Any] = {}
    name = _get_qual_value(attrs, CUSTOMER_NAME)
    if name:
        known[CUSTOMER_NAME] = name
    elif prefs.recipient and _is_plausible_name(prefs.recipient):
        known[CUSTOMER_NAME] = prefs.recipient
    city = _get_qual_value(attrs, SHIPPING_CITY)
    if city:
        known[SHIPPING_CITY] = city
    urgency = _get_qual_value(attrs, URGENCY)
    if urgency:
        known[URGENCY] = urgency
    return known
