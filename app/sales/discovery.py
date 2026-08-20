"""Qualification / discovery state helpers (IQ-08)."""

from __future__ import annotations

from typing import Any

from ..models import IncomingMessage, SalesInterpretation

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
    return known


def _specific_product_lock(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    return any((subject.model, subject.reference, subject.ean))


def _subject_identifiable(interpretation: SalesInterpretation) -> bool:
    subject = interpretation.subject
    return any((subject.product_type, subject.brand, subject.model, subject.reference, subject.ean))


def _persona_requires_qualification() -> bool:
    try:
        from ..persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
    except Exception:
        return False
    return bool(runtime and runtime.enabled and runtime.require_qualification_before_catalog)


_QUAL_STYLE_DIMS = frozenset({"style", "occasion", "material", "color"})
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
    try:
        from ..persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
    except Exception:
        runtime = None
    prompts = list(getattr(runtime, "qualification_prompts", None) or [])
    if not prompts:
        prompts = [
            "Você já tem um modelo em mente ou quer uma sugestão?",
            "Qual faixa de investimento você tem em mente?",
            "É para uso no dia a dia, trabalho, esporte ou uma ocasião especial?",
        ]

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
    if interpretation.subject.brand:
        if not snap.get("has_budget"):
            return (
                f"Beleza, {interpretation.subject.brand}. "
                "Qual faixa de investimento você tem em mente?"
            )
        return (
            f"Beleza, {interpretation.subject.brand}. "
            "Qual estilo você prefere: mergulho, esportivo ou mais clássico?"
        )
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
    for brand in known:
        if brand in folded and brand not in found:
            found.append(brand.title() if brand != "tag heuer" else "TAG Heuer")
    return found


def _comparison_clarification_question(message: IncomingMessage, interpretation: SalesInterpretation) -> str:
    brands = _mentioned_watch_brands(message.text)
    if interpretation.subject.brand and interpretation.subject.brand not in brands:
        brands.insert(0, interpretation.subject.brand)
    if len(brands) >= 2:
        labeled = " e ".join(brands[:2])
        return (
            f"Beleza, {labeled}. Qual modelo de cada um voc\u00ea tem em mente, "
            "ou o que mais pesa agora: estilo, or\u00e7amento ou uso?"
        )
    if brands:
        return (
            f"Beleza, {brands[0]}. Qual modelo voc\u00ea quer comparar, "
            "ou o que mais pesa: estilo, or\u00e7amento ou uso?"
        )
    return (
        "Qual modelo voc\u00ea quer comparar, ou o que mais pesa agora: "
        "estilo, or\u00e7amento ou uso?"
    )


def _discovery_state(
    interpretation: SalesInterpretation,
    recent_turns: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    clarification_count = _consecutive_clarification_count(recent_turns)
    known_preferences = _known_preferences(interpretation)
    explicit_no_preferences = list(dict.fromkeys(interpretation.preferences.explicit_no_preferences))
    known_preferences_count = len(known_preferences) + len(explicit_no_preferences)
    subject_identifiable = _subject_identifiable(interpretation)
    enough_information = interpretation.enough_information_to_search
    comparison_without_sku = _comparison_needs_qualification(interpretation)
    force_retrieval = (
        subject_identifiable
        and not comparison_without_sku
        and any((
            enough_information,
            interpretation.ready_for_retrieval,
            interpretation.stop_clarification,
        ))
    )
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



