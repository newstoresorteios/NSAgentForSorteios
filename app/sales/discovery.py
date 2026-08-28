"""Qualification / discovery state helpers (IQ-08)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..models import IncomingMessage, SalesInterpretation
from ..commerce_context import CommerceConversationState
from ..order_service import has_active_order_context, is_order_lookup_request
from ..context_resume import is_short_affirmation


_OPEN_BROWSE_RE = re.compile(
    r"\b("
    r"quero ver|quero olhar|quero conhecer|quero um|quero uma|"
    r"me mostra|mostra(?:r)?|"
    r"ver modelos?|ver op(?:ç|c)(?:õ|o)es|op(?:ç|c)(?:õ|o)es de|"
    r"sugest(?:ã|a)o|procurando|busco|buscar"
    r")\b",
    flags=re.IGNORECASE,
)
_BUDGET_IN_MESSAGE_RE = re.compile(
    r"("
    r"\bat[eé]\b|\bno m[aá]ximo\b|\bat[eé]\s+\d|\b\d+\s*mil\b|"
    r"\bor[cç]amento\b|\binvestimento\b|\bfaixa\b|\br\$\b|"
    r"\breais\b|\b\d+\s*k\b|\bbudget\b"
    r")",
    flags=re.IGNORECASE,
)

def _is_clarification_turn(turn: dict[str, Any]) -> bool:
    metadata = turn.get("metadata") if isinstance(turn, dict) else None
    return (
        turn.get("role") == "assistant"
        and isinstance(metadata, dict)
        and metadata.get("safety_reason") == "commerce_clarification"
    )


def _consecutive_clarification_count(recent_turns: list[dict[str, Any]] | None) -> int:
    count = 0
    for turn in reversed(recent_turns or []):
        if turn.get("role") == "user":
            continue
        if not _is_clarification_turn(turn):
            break
        count += 1
    return count


def _known_preferences(interpretation: SalesInterpretation) -> dict[str, Any]:
    preferences = interpretation.preferences
    known: dict[str, Any] = {}
    if preferences.budget_min is not None or preferences.budget_max is not None:
        known["budget"] = {
            "min": preferences.budget_min,
            "max": preferences.budget_max,
        }
    for field in ("color", "style", "material", "occasion", "recipient"):
        value = getattr(preferences, field)
        if value:
            known[field] = value
    if interpretation.subject.brand:
        known["brand"] = interpretation.subject.brand
    if preferences.attributes:
        known["attributes"] = preferences.attributes
    try:
        from ..catalog_specs import interpretation_case_size_range

        case_range = interpretation_case_size_range(interpretation)
        if case_range:
            known["case_size"] = f"{case_range[0]}-{case_range[1]}mm"
    except Exception:
        pass
    return known


def _recent_clarification_about_size(recent_turns: list[dict[str, Any]] | None) -> bool:
    for turn in reversed(recent_turns or []):
        if turn.get("role") == "user":
            continue
        if not _is_clarification_turn(turn):
            break
        content = _fold(str(turn.get("content") or ""))
        if any(
            token in content
            for token in ("mm", "caixa", "tamanho", "medida", "pulso", "procurar", "buscar")
        ):
            return True
        break
    return False


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def message_states_budget(text: str | None) -> bool:
    return bool(_BUDGET_IN_MESSAGE_RE.search(str(text or "")))


def is_open_catalog_browse_request(
    text: str | None,
    interpretation: SalesInterpretation,
) -> bool:
    """Fresh brand/type browse — not a deictic pick from a prior shortlist."""
    if _specific_product_lock(interpretation):
        return False
    if not (interpretation.subject.brand or interpretation.subject.product_type):
        return False
    if interpretation.goal in {"compare", "buy", "inspect"}:
        return False
    folded = str(text or "").casefold()
    if any(
        token in folded
        for token in (
            "esse",
            "essa",
            "deste",
            "desta",
            "primeiro",
            "segundo",
            "terceiro",
            "o 1",
            "o 2",
            "o 3",
            "número",
            "numero",
        )
    ):
        return False
    return bool(_OPEN_BROWSE_RE.search(str(text or "")))


def _scrub_stale_budget_for_open_browse(
    known_preferences: dict[str, Any],
    *,
    interpretation: SalesInterpretation,
    message_text: str | None,
) -> dict[str, Any]:
    """Don't let an old budget from chat memory skip Crono's investment question."""
    if not is_open_catalog_browse_request(message_text, interpretation):
        return known_preferences
    if message_states_budget(message_text):
        return known_preferences
    if "budget" not in known_preferences:
        return known_preferences
    cleaned = dict(known_preferences)
    cleaned.pop("budget", None)
    print(
        "[sales.discovery.scrub_stale_budget]",
        {
            "brand": interpretation.subject.brand,
            "goal": interpretation.goal,
            "had_budget": True,
        },
    )
    return cleaned


def _specific_product_lock(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    if subject.reference or subject.ean:
        return True
    model = str(subject.model or "").strip()
    if not model:
        return False
    model_fold = model.casefold()
    brand_fold = str(subject.brand or "").casefold()
    if brand_fold and model_fold == brand_fold:
        return False
    # Brand-only (or brand + "relógio") misparsed as model must not unlock catalog.
    leftover = model_fold
    for hit in _mentioned_watch_brands(model):
        leftover = leftover.replace(hit.casefold(), " ")
    for token in ("relógio", "relogio", "watch"):
        leftover = leftover.replace(token, " ")
    leftover = " ".join(leftover.split())
    return bool(leftover)


def _subject_identifiable(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    return any((subject.product_type, subject.brand, subject.model, subject.reference, subject.ean))


def _persona_requires_qualification() -> bool:
    """Keep ChatBo gate when persona DB load fails; honor flag when runtime is healthy."""
    try:
        from ..persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
    except Exception:
        return True
    # Outside a request scope (unit tests), no bound runtime → do not force the gate.
    if runtime is None:
        return False
    if getattr(runtime, "load_error", None):
        return True
    if not getattr(runtime, "enabled", False):
        return False
    return bool(getattr(runtime, "require_qualification_before_catalog", True))


# Only real style/occasion unlock brand+style. Color/material are catalog filters
# (e.g. "dourado", "automático") and must NOT skip Crono's qualification questions.
_QUAL_STYLE_DIMS = frozenset({"style", "occasion"})
_QUAL_BUDGET_DIMS = frozenset({"budget"})
_QUAL_MAX_QUESTIONS = 2


def _preference_key_set(discovery_state: dict[str, Any]) -> set[str]:
    known = discovery_state.get("known_preferences") or []
    if isinstance(known, dict):
        return set(known.keys())
    return set(known)


def _has_urgency_signal(interpretation: SalesInterpretation) -> bool:
    attrs = list(interpretation.preferences.attributes or [])
    blob = " ".join(str(item) for item in attrs).casefold()
    return any(
        token in blob
        for token in ("pronta", "urgência", "urgencia", "rápido", "rapido", "hoje", "amanhã", "amanha")
    )


def build_qualification_snapshot(
    interpretation: SalesInterpretation,
    discovery_state: dict[str, Any],
) -> dict[str, Any]:
    """State machine: which ChatBo qualification dims are covered and if catalog may open."""
    known = _preference_key_set(discovery_state)
    explicit_no = set(discovery_state.get("explicit_no_preferences") or [])
    covered = known | explicit_no
    has_brand = bool(interpretation.subject.brand) or "brand" in covered
    has_product_type = bool(interpretation.subject.product_type)
    # Budget/style come from known_preferences (includes rehydrated prefs).
    # Open-browse scrub removes stale budget from known_preferences on purpose —
    # do not re-read interpretation.preferences here or scrub is bypassed.
    has_budget = bool(covered & _QUAL_BUDGET_DIMS)
    has_style = bool(covered & _QUAL_STYLE_DIMS)
    has_recipient = "recipient" in covered
    has_urgency = _has_urgency_signal(interpretation)
    clarification_count = int(discovery_state.get("clarification_count") or 0)

    required = _persona_requires_qualification()
    satisfied_by: str | None = None
    ready = False

    if not required:
        ready = True
        satisfied_by = "persona_qualification_off"
    elif interpretation.stop_clarification:
        ready = True
        satisfied_by = "stop_clarification"
    elif _specific_product_lock(interpretation):
        ready = True
        satisfied_by = "sku_lock"
    elif clarification_count >= _QUAL_MAX_QUESTIONS:
        ready = True
        satisfied_by = "max_questions"
    elif has_brand and has_budget:
        ready = True
        satisfied_by = "brand+budget"
    elif has_brand and has_style:
        ready = True
        satisfied_by = "brand+style"
    elif has_brand and has_urgency:
        ready = True
        satisfied_by = "brand+urgency"
    elif has_product_type and has_budget and has_style:
        ready = True
        satisfied_by = "type+budget+style"
    elif has_product_type and has_budget and (has_recipient or has_urgency):
        ready = True
        satisfied_by = "type+budget+signal"

    missing: list[str] = []
    if not has_budget:
        missing.append("budget")
    if not has_style:
        missing.append("style")
    if not has_brand and not has_product_type:
        missing.append("subject")
    if has_brand and not has_budget and not has_style and not has_urgency:
        # Still need one unlocking signal beyond brand.
        pass

    return {
        "required": required,
        "ready": ready,
        "satisfied_by": satisfied_by,
        "covered_dims": sorted(covered),
        "missing_dims": missing,
        "has_brand": has_brand,
        "has_product_type": has_product_type,
        "has_budget": has_budget,
        "has_style": has_style,
        "has_urgency": has_urgency,
        "clarification_count": clarification_count,
        "max_questions": _QUAL_MAX_QUESTIONS,
    }


def _persona_qualification_question(
    interpretation: SalesInterpretation,
    discovery_state: dict[str, Any] | None = None,
) -> str | None:
    """Pick the next unused qualification prompt from the active persona only.

    Containment (which dim is missing) lives in code; the spoken question text
    must come from ChatBo/persona — never invent reply copy here.
    """
    try:
        from ..persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
    except Exception:
        runtime = None
    prompts = [
        str(item).strip()
        for item in (getattr(runtime, "qualification_prompts", None) or [])
        if str(item or "").strip()
    ]
    if not prompts:
        return None

    # Prefer real questions from the persona list (skip bare field labels).
    question_like = [
        item
        for item in prompts
        if "?" in item
        or item.casefold().startswith(
            ("como ", "qual ", "você ", "voce ", "é ", "e ", "para ")
        )
    ]
    if question_like:
        prompts = question_like

    snap = (discovery_state or {}).get("qualification")
    if not isinstance(snap, dict):
        snap = build_qualification_snapshot(interpretation, discovery_state or {})

    known = set(snap.get("covered_dims") or [])
    recent = [
        str(item or "").casefold()
        for item in ((discovery_state or {}).get("recent_questions") or [])
    ]

    def _unused(prompt: str) -> bool:
        folded = prompt.casefold()
        return not any(folded[:48] in previous or previous[:48] in folded for previous in recent)

    # Priority order depends on what still unlocks the state machine.
    preference_hints: list[tuple[str, tuple[str, ...]]] = []
    if not snap.get("has_budget"):
        preference_hints.append(
            ("budget", ("investimento", "orçamento", "orcamento", "faixa"))
        )
    if not snap.get("has_style"):
        preference_hints.append(
            (
                "style",
                ("estilo", "esporte", "ocasião", "ocasiao", "dia a dia", "trabalho", "uso"),
            )
        )
        preference_hints.append(("occasion", ("ocasião", "ocasiao", "presente", "especial")))
    if not snap.get("has_brand") and not snap.get("has_product_type"):
        preference_hints.append(
            ("model_intent", ("modelo em mente", "sugestão", "sugestao", "marca"))
        )
    preference_hints.append(("recipient", ("chamar", "nome", "para quem", "cidade")))

    for field, needles in preference_hints:
        if field in known and field not in {"model_intent"}:
            continue
        for prompt in prompts:
            folded = prompt.casefold()
            if any(needle in folded for needle in needles) and _unused(prompt):
                return prompt

    for prompt in prompts:
        if _unused(prompt):
            return prompt
    return prompts[0] if prompts else None


def _needs_persona_qualification(
    interpretation: SalesInterpretation,
    discovery_state: dict[str, Any],
) -> bool:
    snapshot = build_qualification_snapshot(interpretation, discovery_state)
    discovery_state["qualification"] = snapshot
    return bool(snapshot["required"] and not snapshot["ready"])


def _comparison_needs_qualification(interpretation: SalesInterpretation) -> bool:
    return interpretation.goal == "compare" and not _specific_product_lock(interpretation)


def _mentioned_watch_brands(text: str | None) -> list[str]:
    folded = unicodedata.normalize("NFKD", (text or "").casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    known = (
        "hamilton",
        "baltic",
        "tissot",
        "citizen",
        "seiko",
        "bulova",
        "orient",
        "casio",
        "mido",
        "omega",
        "longines",
        "oris",
        "certina",
        "tudor",
        "zenith",
        "breitling",
        "panerai",
        "iwc",
        "rolex",
        "tag heuer",
        "christopher ward",
    )
    found: list[str] = []
    display = {
        "tag heuer": "TAG Heuer",
        "christopher ward": "Christopher Ward",
        "bulova": "Bulova",
        "orient": "Orient",
        "casio": "Casio",
        "mido": "Mido",
    }
    for brand in known:
        if brand in folded and brand not in found:
            found.append(display.get(brand, brand.title()))
    return found


def _comparison_clarification_question(message: IncomingMessage, interpretation: SalesInterpretation) -> str:
    """Containment for compare-without-SKU: prefer a persona qualification prompt."""
    persona_q = _persona_qualification_question(interpretation, {})
    if persona_q:
        return persona_q
    # No persona prompts loaded — ask interpreter/LLM path instead of inventing copy.
    return (interpretation.clarification_question or "").strip()


def _discovery_state(
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None,
    *,
    message_text: str | None = None,
    commerce_state: CommerceConversationState | None = None,
) -> dict[str, Any]:
    clarification_count = _consecutive_clarification_count(recent_turns)
    known_preferences = _scrub_stale_budget_for_open_browse(
        _known_preferences(interpretation),
        interpretation=interpretation,
        message_text=message_text,
    )
    explicit_no_preferences = list(dict.fromkeys(interpretation.preferences.explicit_no_preferences))
    known_preferences_count = len(known_preferences) + len(explicit_no_preferences)
    subject_identifiable = _subject_identifiable(interpretation)
    enough_information = interpretation.enough_information_to_search
    # Open browse without budget in this message must re-qualify even if the
    # interpreter inherited enough_information / ready_for_retrieval from memory.
    if (
        is_open_catalog_browse_request(message_text, interpretation)
        and not message_states_budget(message_text)
        and "budget" not in known_preferences
    ):
        enough_information = False
    comparison_without_sku = _comparison_needs_qualification(interpretation)
    try:
        from ..catalog_specs import (
            interpretation_case_size_range,
            message_requests_other_brands,
        )

        case_range = interpretation_case_size_range(
            interpretation,
            message_text=message_text,
        )
        brand_unlock = message_requests_other_brands(message_text)
    except Exception:
        case_range = None
        brand_unlock = False
    force_retrieval = (
        subject_identifiable
        and not comparison_without_sku
        and any((
            enough_information,
            interpretation.ready_for_retrieval,
            interpretation.stop_clarification,
        ))
    )
    if case_range and subject_identifiable:
        force_retrieval = True
        enough_information = True
    if (
        is_short_affirmation(message_text)
        and _recent_clarification_about_size(recent_turns)
        and subject_identifiable
        and known_preferences_count >= 2
    ):
        force_retrieval = True
        enough_information = True
    recent_questions = [
        str(turn.get("content") or "").strip()
        for turn in recent_turns or []
        if _is_clarification_turn(turn) and str(turn.get("content") or "").strip()
    ][-5:]
    preference_fields = {"budget", "brand", "color", "style", "material", "occasion", "recipient", "attributes"}
    unknown_preferences = sorted(
        preference_fields - set(known_preferences) - set(explicit_no_preferences)
    )
    state = {
        "clarification_count": clarification_count,
        "enough_information_to_search": enough_information,
        "ready_for_retrieval": interpretation.ready_for_retrieval,
        "stop_clarification": interpretation.stop_clarification,
        "known_preferences": known_preferences,
        "known_preferences_count": known_preferences_count,
        "unknown_preferences": unknown_preferences,
        "explicit_no_preferences": explicit_no_preferences,
        "recent_questions": recent_questions,
        "subject_identifiable": subject_identifiable,
        "force_retrieval": force_retrieval,
        "comparison_without_sku": comparison_without_sku,
        "persona_qualification_required": False,
        "case_size_range": case_range,
        "brand_unlock_requested": brand_unlock,
        "order_context_blocks_clarification": (
            has_active_order_context(commerce_state)
            and is_order_lookup_request(
                message_text,
                commerce_state=commerce_state,
            )
        ),
    }
    if _needs_persona_qualification(interpretation, state):
        state["force_retrieval"] = False
        state["persona_qualification_required"] = True
    return state


def _needs_clarification_before_retrieval(
    interpretation: SalesInterpretation,
    plan: dict[str, Any],
    discovery_state: dict[str, Any],
) -> bool:
    if discovery_state.get("order_context_blocks_clarification"):
        return False
    if discovery_state.get("persona_qualification_required"):
        return True
    if discovery_state["force_retrieval"]:
        return False
    if plan.get("intent") == "purchase_intent":
        return True
    if interpretation.needs_clarification or interpretation.goal == "discover":
        return True
    if _comparison_needs_qualification(interpretation) or plan.get("intent") == "product_comparison":
        if not _specific_product_lock(interpretation):
            return True
    if plan.get("intent") not in {"purchase_intent", "recommendation"}:
        return False
    return not discovery_state["subject_identifiable"]



