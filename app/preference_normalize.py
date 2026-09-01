"""Normalize interpreter preferences (gender, budget) before catalog retrieval."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import ProductPreferences, SalesInterpretation

_FEMALE_TERMS = frozenset(
    {
        "feminino",
        "feminina",
        "femininos",
        "femininas",
        "mulher",
        "mulheres",
        "dama",
        "damas",
        "lady",
        "ladies",
        "women",
        "woman",
        "ela",
        "esposa",
        "namorada",
        "mae",
        "mãe",
        "filha",
        "senhora",
        "senhoras",
    }
)
_MALE_TERMS = frozenset(
    {
        "masculino",
        "masculina",
        "masculinos",
        "masculinas",
        "homem",
        "homens",
        "men",
        "man",
        "ele",
        "marido",
        "namorado",
        "pai",
        "filho",
        "senhor",
        "senhores",
    }
)
_UNISEX_TERMS = frozenset({"unissex", "unisex", "neutro", "neutra"})

_GENDER_ALIASES: dict[str, tuple[str, ...]] = {
    "feminino": (
        "feminino",
        "feminina",
        "lady",
        "ladies",
        "women",
        "woman",
        "dama",
        "damas",
    ),
    "masculino": (
        "masculino",
        "masculina",
        "men",
        "man",
        "homem",
        "homens",
    ),
    "unissex": ("unissex", "unisex"),
}


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(value))


def detect_gender_label(*parts: Any) -> str | None:
    """Return canonical feminino|masculino|unissex from free text fragments."""
    found: set[str] = set()
    for part in parts:
        for token in _tokens(part):
            if token in _FEMALE_TERMS or token.startswith("femin"):
                found.add("feminino")
            elif token in _MALE_TERMS or token.startswith("mascul"):
                found.add("masculino")
            elif token in _UNISEX_TERMS:
                found.add("unissex")
    if "feminino" in found and "masculino" not in found:
        return "feminino"
    if "masculino" in found and "feminino" not in found:
        return "masculino"
    if found == {"unissex"}:
        return "unissex"
    return None


def gender_search_aliases(label: str | None) -> tuple[str, ...]:
    if not label:
        return ()
    return _GENDER_ALIASES.get(label, (label,))


def preference_gender_label(interpretation: SalesInterpretation) -> str | None:
    preferences = interpretation.preferences
    return detect_gender_label(
        preferences.recipient,
        preferences.style,
        preferences.occasion,
        *preferences.attributes,
        interpretation.subject.model,
        interpretation.subject.product_type,
    )


def _strip_gender_tokens(value: str | None) -> str | None:
    if not value:
        return None
    kept = [
        token
        for token in re.findall(r"[A-Za-zÀ-ú0-9]+", value)
        if _fold(token) not in (_FEMALE_TERMS | _MALE_TERMS | _UNISEX_TERMS)
        and not _fold(token).startswith(("femin", "mascul"))
    ]
    cleaned = " ".join(kept).strip(" ,-")
    return cleaned or None


def is_gender_only_label(value: str | None) -> bool:
    """True when the whole string is only a gender cue (never a product model)."""
    if not value or not str(value).strip():
        return False
    return detect_gender_label(value) is not None and _strip_gender_tokens(value) is None


def _ensure_attribute(preferences: ProductPreferences, label: str) -> None:
    existing = {_fold(item) for item in preferences.attributes}
    if _fold(label) not in existing:
        preferences.attributes = [*preferences.attributes, label]


def _extract_budget_max(text: str) -> float | None:
    match = re.search(
        r"(?:at[eé]|ate|menos de|no m[aá]ximo|at[eé] uns?|por at[eé])\s*"
        r"(?:r\$\s*)?([\d.,]+)\s*(mil|k)?",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:r\$\s*)?([\d.,]+)\s*(mil|k)?\s*(?:reais|real)?",
            text or "",
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        # Avoid treating bare years/codes as budget without currency cue.
        raw_text = (text or "").lower()
        if "real" not in raw_text and "r$" not in raw_text and "mil" not in raw_text:
            if not re.search(r"at[eé]|ate|menos|m[aá]ximo", raw_text):
                return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if match.group(2):
        value *= 1000
    if value <= 0 or value > 1_000_000:
        return None
    return value


_DIAL_HINT_RE = re.compile(
    r"\b(?:visor|mostrador|dial)\s+(?:em\s+|na\s+|de\s+|com\s+)?"
    r"(?P<dial>preto|black|branco|white|azul|blue|verde|green|rosa|pink|"
    r"vermelho|red|cinza|gray|grey|dourado|gold|prata|silver)\b",
    flags=re.IGNORECASE,
)
_CASE_GOLD_RE = re.compile(
    r"\b(?P<case>dourad[oa]|gold|golden|ouro)\b",
    flags=re.IGNORECASE,
)
_CASE_STEEL_RE = re.compile(
    r"\b(?P<case>prata|silver|a[cç]o|steel|inox)\b",
    flags=re.IGNORECASE,
)
_INTEGRATED_BRACELET_RE = re.compile(
    r"(?:pulseira|caixa)\s+integrad|integrad[ao]\s+(?:com\s+)?(?:a\s+)?pulseira",
    flags=re.IGNORECASE,
)
_BRUSHED_CASE_RE = re.compile(
    r"caixa\s+(?:rajad[ao]|escovad[ao])|"
    r"(?:rajad[ao]|escovad[ao])\s+(?:na\s+)?caixa|"
    r"acabamento\s+escovad[ao]|brushed\s+case",
    flags=re.IGNORECASE,
)
_PRX_RE = re.compile(r"\bprx\b", flags=re.IGNORECASE)


def recent_user_context_text(
    recent_turns: list[dict[str, Any]] | None,
    *,
    limit: int = 8,
) -> str:
    parts: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict) or turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n".join(parts[-limit:])


def repair_style_preferences(
    preferences: ProductPreferences,
    subject: Any,
    *,
    message_text: str | None = None,
    context_text: str | None = None,
) -> None:
    """Extract PRX integrated bracelet / brushed case cues spread across turns."""
    combined = "\n".join(
        part for part in (context_text or "", message_text or "") if part
    )
    folded = _fold(combined)
    if not folded:
        return

    if _INTEGRATED_BRACELET_RE.search(combined):
        _ensure_attribute(preferences, "pulseira_integrada")
    if _BRUSHED_CASE_RE.search(combined) or (
        "caixa" in folded and ("rajad" in folded or "escovad" in folded)
    ):
        _ensure_attribute(preferences, "acabamento_escovado")
        material_fold = _fold(preferences.material)
        color_fold = _fold(preferences.color)
        if not material_fold or material_fold in {"aco", "steel", "inox"}:
            if color_fold not in {"preto", "black"}:
                preferences.material = "prata"
    if _PRX_RE.search(combined):
        model_fold = _fold(getattr(subject, "model", None))
        if not model_fold or model_fold in {"relogio", "watch", "tissot"}:
            subject.model = "PRX"
        elif "prx" not in model_fold:
            subject.model = f"{subject.model} PRX".strip()


def repair_dial_and_case_preferences(
    preferences: ProductPreferences,
    *,
    message_text: str | None = None,
) -> ProductPreferences:
    """Split 'dourado com visor preto' into dial=preto + material=dourado."""
    text = message_text or ""
    folded = _fold(text)
    if not folded:
        return preferences

    dial_match = _DIAL_HINT_RE.search(text)
    dial = _fold(dial_match.group("dial")) if dial_match else None
    case = None
    gold = _CASE_GOLD_RE.search(text)
    steel = _CASE_STEEL_RE.search(text)
    if gold:
        case = "dourado"
    elif steel and dial and _fold(steel.group("case")) != dial:
        case = "prata"

    color_fold = _fold(preferences.color)
    material_fold = _fold(preferences.material)

    # Visor/mostrador always wins as dial when present.
    if dial and color_fold != dial:
        # If color was the case finish (dourado) and dial is preto, move gold to material.
        if color_fold in {"dourado", "gold", "golden", "ouro"} and not material_fold:
            preferences.material = preferences.color or "dourado"
        preferences.color = dial
        color_fold = dial

    # Gold/steel mentioned alongside a different dial → case finish.
    if case and (not material_fold or material_fold in {"aco", "steel", "inox"}):
        if not dial or case != color_fold:
            preferences.material = case

    # Collapse "dourado preto" without visor cue: keep preto as dial if both present.
    if not dial and color_fold:
        tokens = _tokens(preferences.color)
        if "preto" in tokens and any(t in tokens for t in ("dourado", "gold", "ouro")):
            preferences.color = "preto"
            if not preferences.material:
                preferences.material = "dourado"

    return preferences


_MODEL_LINE_RE = re.compile(
    r"\b(mk\s*2|mk2|mr\s*0?1|aquascaphe|speedtimer|king\s+turtle|samurai)\b",
    flags=re.IGNORECASE,
)
_SINGLE_MM_RE = re.compile(r"\b(3[0-9]|4[0-5])\s*mm\b", re.IGNORECASE)


def repair_specific_model_tokens(
    subject: Any,
    preferences: ProductPreferences,
    *,
    message_text: str | None = None,
    context_text: str | None = None,
) -> None:
    """Lock Baltic mk2 / explicit mm into subject + attributes for sku_lock."""
    combined = "\n".join(
        part for part in (context_text or "", message_text or "") if part
    )
    folded = _fold(combined)
    if not folded:
        return

    brand_fold = _fold(subject.brand)
    if "baltic" in folded and not brand_fold:
        subject.brand = "Baltic"
        brand_fold = "baltic"

    model_fold = _fold(subject.model)
    line_match = _MODEL_LINE_RE.search(combined)
    if line_match:
        token = _fold(line_match.group(1)).replace(" ", "")
        if token in {"mk2", "mk02"}:
            token = "mk2"
        if not model_fold or model_fold in {brand_fold, "relogio", "watch"}:
            subject.model = "Aquascaphe mk2" if token == "mk2" else token
        elif token not in model_fold:
            subject.model = f"{subject.model} {token}".strip()

    mm_match = _SINGLE_MM_RE.search(combined)
    try:
        from .catalog_specs import extract_case_size_range_from_text

        if extract_case_size_range_from_text(combined):
            mm_match = None
    except Exception:
        pass
    if mm_match:
        size = int(mm_match.group(1))
        label = f"case_size:{size}-{size}mm"
        existing = {_fold(item) for item in preferences.attributes}
        if _fold(label) not in existing:
            preferences.attributes = [*preferences.attributes, label]


def normalize_sales_interpretation(
    interpretation: SalesInterpretation,
    *,
    message_text: str | None = None,
    context_text: str | None = None,
    recent_turns: list[dict[str, Any]] | None = None,
) -> SalesInterpretation:
    """Fix gender misclassified as model/style and keep recommendation mode."""
    try:
        from .sales.qualification_slots import rehydrate_qualification_slots_from_turns

        interpretation = rehydrate_qualification_slots_from_turns(
            interpretation,
            recent_turns,
            message_text=message_text,
        )
    except Exception:
        pass
    preferences = interpretation.preferences
    subject = interpretation.subject
    combined_context = "\n".join(
        part for part in (context_text or "", message_text or "") if part
    )
    gender = detect_gender_label(
        combined_context,
        preferences.recipient,
        preferences.style,
        preferences.occasion,
        *preferences.attributes,
        subject.model,
        subject.product_type,
    )
    if gender:
        preferences.recipient = gender
        _ensure_attribute(preferences, gender)
        # Gender is never a catalog model identity — that forces exact mode and fails.
        if detect_gender_label(subject.model) == gender and not _strip_gender_tokens(
            subject.model
        ):
            subject.model = None
        else:
            subject.model = _strip_gender_tokens(subject.model)
        style_gender = detect_gender_label(preferences.style)
        if style_gender == gender:
            preferences.style = _strip_gender_tokens(preferences.style)

    # Pull budget from free text when interpreter missed it.
    if preferences.budget_max is None and message_text:
        budget = _extract_budget_max(message_text)
        if budget is not None:
            preferences.budget_max = budget

    repair_dial_and_case_preferences(preferences, message_text=combined_context)
    repair_style_preferences(
        preferences,
        subject,
        message_text=message_text,
        context_text=context_text,
    )
    repair_specific_model_tokens(
        subject,
        preferences,
        message_text=message_text,
        context_text=combined_context,
    )

    try:
        from .sales.discovery import _specific_product_lock

        if _specific_product_lock(interpretation):
            interpretation.enough_information_to_search = True
            interpretation.ready_for_retrieval = True
            interpretation.stop_clarification = True
            interpretation.needs_clarification = False
            if interpretation.goal in {None, "discover"}:
                interpretation.goal = "find"
    except Exception:
        pass

    try:
        from .catalog_specs import (
            extract_case_size_range_from_text,
            message_requests_other_brands,
            message_wants_chronograph,
            apply_brand_unlock_to_interpretation,
        )

        case_range = extract_case_size_range_from_text(combined_context)
        if case_range:
            label = f"case_size:{case_range[0]}-{case_range[1]}mm"
            if label not in list(preferences.attributes or []):
                preferences.attributes = list(preferences.attributes or []) + [label]
            interpretation.enough_information_to_search = True
            interpretation.ready_for_retrieval = True
            interpretation.stop_clarification = True
            interpretation.needs_clarification = False
            if interpretation.goal in {None, "discover"}:
                interpretation.goal = "recommend"

        rejected = apply_brand_unlock_to_interpretation(
            interpretation,
            message_text=message_text or context_text,
        )
        if rejected or message_requests_other_brands(message_text or context_text):
            interpretation.stop_clarification = True
            interpretation.ready_for_retrieval = True
            interpretation.needs_clarification = False
            interpretation.enough_information_to_search = True
            if interpretation.goal in {None, "discover"}:
                interpretation.goal = "recommend"

        if message_wants_chronograph(combined_context):
            attrs = list(preferences.attributes or [])
            if "cronógrafo" not in attrs and "cronografo" not in {_fold(a) for a in attrs}:
                attrs.append("cronógrafo")
            preferences.attributes = attrs
            if not preferences.style or _fold(preferences.style) in {
                "diver",
                "mergulho",
                "versatil",
                "versátil",
            }:
                # Chronograph is a stronger current intent than a soft style leftover.
                if message_wants_chronograph(message_text):
                    preferences.style = "cronógrafo"
            if not subject.product_type:
                subject.product_type = "relógio"
            interpretation.enough_information_to_search = True
            interpretation.ready_for_retrieval = True
            interpretation.stop_clarification = True
            interpretation.needs_clarification = False
            if interpretation.goal in {None, "discover"}:
                interpretation.goal = "recommend"

        from .context_resume import is_short_affirmation

        if is_short_affirmation(message_text) and not case_range:
            case_range = extract_case_size_range_from_text(context_text)
            if case_range:
                label = f"case_size:{case_range[0]}-{case_range[1]}mm"
                if label not in list(preferences.attributes or []):
                    preferences.attributes = list(preferences.attributes or []) + [label]
                interpretation.enough_information_to_search = True
                interpretation.ready_for_retrieval = True
                interpretation.stop_clarification = True
                interpretation.needs_clarification = False
    except Exception:
        pass

    # After gender + budget, discovery answers are usually ready to search.
    if (
        interpretation.domain == "commerce"
        and subject.product_type
        and gender
        and interpretation.goal in {None, "discover", "recommend", "find"}
        and not interpretation.needs_clarification
    ):
        interpretation.enough_information_to_search = True
        interpretation.ready_for_retrieval = True
        if interpretation.goal in {None, "discover"}:
            interpretation.goal = "recommend"

    if (
        interpretation.domain == "commerce"
        and subject.product_type
        and (preferences.budget_max is not None or preferences.budget_min is not None)
        and gender
    ):
        interpretation.enough_information_to_search = True
        interpretation.ready_for_retrieval = True
        interpretation.needs_clarification = False
        if interpretation.goal in {None, "discover"}:
            interpretation.goal = "recommend"

    return interpretation
