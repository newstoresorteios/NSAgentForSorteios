from __future__ import annotations

import re
from typing import Any

from app.models import SalesInterpretation
from app.catalog.retrieval.aliases import (
    _ACCESSORY_COLOR_TOKENS,
    _ACCESSORY_NAME_TOKENS,
    _COLOR_ALIAS_GROUPS,
    _DESCRIPTOR_MODEL_TOKENS,
    _DIAL_COLOR_RIVAL_TOKENS,
    _DIAL_COLOR_TOKENS,
    _FEATURE_SEARCH_ALIASES,
    _MODEL_STOPWORDS,
    _OPTIONAL_MODEL_TOKENS,
    _PROMASTER_SKY_MISLABELS,
    _PROMASTER_SKY_PILOT_ALIASES,
    _PROSPEX_DIVER_COMMERCIAL_ALIASES,
    _PROSPEX_DIVER_SOFT_IDENTITY,
)
from app.catalog.retrieval.limits import CUSTOMER_RESULT_LIMIT
from app.catalog.retrieval.text import _fold, _product_text

_REFERENCE_CODE_RE = re.compile(
    r"\b("
    # Citizen/Seiko style: JV2000-51L, SRPE37K1-A (letter+digits with one hyphen+)
    r"[A-Z]{1,4}\d{2,}[A-Z0-9]*(?:-[A-Z0-9]{1,})+"
    r"|"
    r"[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,}"
    r"|"
    r"[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){2,}"
    r")\b",
    re.IGNORECASE,
)
# Alphanumeric model codes with digits: PH2000M, C63, SUB300, etc.
_MODEL_CODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{3,}\b"
)


def significant_model_tokens(model: str | None) -> tuple[str, ...]:
    """Core tokens for exact matching — drops filler words like 'automático'."""
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _fold(model))
        if token and token not in _MODEL_STOPWORDS and len(token) >= 2
    ]
    if tokens:
        return tuple(dict.fromkeys(tokens))
    fallback = [token for token in re.findall(r"[a-z0-9]+", _fold(model)) if token]
    return tuple(dict.fromkeys(fallback))


def required_model_tokens(model: str | None) -> tuple[str, ...]:
    """Identity tokens for matching; color/material are optional when possible."""
    tokens = significant_model_tokens(model)
    identity = tuple(
        token for token in tokens if token not in _OPTIONAL_MODEL_TOKENS
    )
    if len(identity) >= 2 or any(re.search(r"\d", token) for token in identity):
        return identity
    # For single-word models (Sealander), keep color tokens for ranking/disambiguation
    # but never require dial descriptors invented by Vision ("claro", "mostrador").
    tightened = tuple(
        token for token in tokens if token not in _DESCRIPTOR_MODEL_TOKENS
    )
    return tightened or identity or tokens


def identity_core_tokens(
    model: str | None,
    *,
    color_tokens: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Model tokens for Tray probes — never mix hue words into the core query."""
    required = required_model_tokens(model)
    drop = set(color_tokens) | _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS
    core = tuple(token for token in required if token not in drop)
    # Prospex store titles use Sea Samurai / King Turtle — dial "Diver's 200m"
    # must not AND-block those catalog rows.
    if any(token == "prospex" for token in core):
        core = tuple(
            token for token in core if token not in _PROSPEX_DIVER_SOFT_IDENTITY
        )
    if core:
        return core
    # Fall back to non-color identity even for single-token models.
    identity = tuple(
        token
        for token in significant_model_tokens(model)
        if token not in drop
    )
    if any(token == "prospex" for token in identity):
        identity = tuple(
            token for token in identity if token not in _PROSPEX_DIVER_SOFT_IDENTITY
        )
    return identity or required


def _rejects_as_accessory(
    product: dict[str, Any],
    identity_tokens: tuple[str, ...],
) -> bool:
    """Single-token model asks must not match straps/kits."""
    if len(identity_tokens) != 1:
        return False
    candidate_model = _fold(product.get("model"))
    candidate_name = _fold(product.get("name"))
    name_tokens = set(re.findall(r"[a-z0-9]+", candidate_name))
    if candidate_model:
        return False
    return bool(name_tokens & _ACCESSORY_NAME_TOKENS)



def is_plausible_product_reference(value: str | None) -> bool:
    """True when value looks like a commercial SKU/ref, not a color description."""
    text = str(value or "").strip()
    if not text:
        return False
    if extract_reference_code(text):
        return True
    folded = _fold(text)
    tokens = [token for token in re.findall(r"[a-z0-9]+", folded) if token]
    if not tokens:
        return False
    soft = _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS | _MODEL_STOPWORDS
    if all(token in soft for token in tokens):
        return False
    # Real refs almost always mix letters/digits with separators.
    if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", text) and re.search(
        r"[\.\-_/]",
        text,
    ):
        return len(text) >= 6
    return False


def effective_product_reference(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if is_plausible_product_reference(text):
        return extract_reference_code(text) or text
    return None


def normalize_pt_catalog_query(text: str | None) -> str:
    """Map common English vision terms to Tray catalog Portuguese."""
    value = str(text or "").strip()
    if not value:
        return ""
    replacements = (
        (r"\bautomatic\b", "Automático"),
        (r"\bchronograph\b", "Cronógrafo"),
        (r"\bquartz\b", "Quartz"),
        (r"\bpink\b", "Rosa"),
        (r"\bblue\b", "Azul"),
        (r"\bgreen\b", "Verde"),
        (r"\bblack\b", "Preto"),
        (r"\bwhite\b", "Branco"),
    )
    updated = value
    for pattern, replacement in replacements:
        updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)
    # Dial text → NewStore commercial title (SRPL13K1 / Sea Samurai line).
    folded = _fold(updated)
    if is_prospex_diver_ask(updated) and "samurai" not in folded and "turtle" not in folded:
        auto = " Automático" if "automatico" in folded else ""
        return f"Prospex Sea Samurai{auto}".strip()
    # Citizen Promaster aviation ana-digi → Sky Pilot (not Navihawk).
    if is_promaster_sky_pilot_ask(updated) and "sky" not in folded:
        hue = " Azul" if any(token in folded for token in ("azul", "blue", "navy")) else ""
        return f"Promaster Sky Pilot{hue}".strip()
    if any(label in folded for label in _PROMASTER_SKY_MISLABELS):
        hue = " Azul" if any(token in folded for token in ("azul", "blue", "navy")) else ""
        return f"Promaster Sky Pilot{hue}".strip()
    return updated


def expand_color_aliases(token: str | None) -> frozenset[str]:
    """Expand a color word to PT/EN synonyms used in Tray titles."""
    folded = _fold(token)
    if not folded:
        return frozenset()
    for group in _COLOR_ALIAS_GROUPS:
        if folded in group:
            return group
    return frozenset({folded})


def preference_color_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Dial-color tokens only — never strap/case materials from Vision dumps."""
    color = _fold(interpretation.preferences.color)
    if not color:
        # Recover color adjectives embedded in the model string from Vision.
        color = " ".join(
            token
            for token in significant_model_tokens(interpretation.subject.model)
            if token in _OPTIONAL_MODEL_TOKENS
            and token not in _DESCRIPTOR_MODEL_TOKENS
            and token not in _ACCESSORY_COLOR_TOKENS
        )
    raw = [
        token
        for token in re.findall(r"[a-z0-9]+", color)
        if token
        and token not in _DESCRIPTOR_MODEL_TOKENS
        and token not in _ACCESSORY_COLOR_TOKENS
    ]
    # Prefer known dial hues; if Vision only sent accessories, require nothing.
    dial = [token for token in raw if token in _DIAL_COLOR_TOKENS]
    if dial:
        # One dial hue is enough for AND/require_color (branco, not branco+bege).
        return (dial[0],)
    return ()


def preference_color_search_labels(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """All alias spellings to probe Tray name filters (azul → azul, blue, …)."""
    tokens = preference_color_tokens(interpretation)
    if not tokens:
        return ()
    labels: list[str] = []
    for token in tokens:
        for alias in sorted(expand_color_aliases(token)):
            labels.append(alias)
    return tuple(dict.fromkeys(labels))

def catalog_match_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Significant AND-search tokens for Tray token/ILIKE lookup."""
    subject = interpretation.subject
    tokens: list[str] = []
    for part in (subject.brand or "").split():
        folded = _fold(part)
        if folded and len(folded) >= 2:
            tokens.append(folded)
    color_tokens = preference_color_tokens(interpretation)
    # Strip accessory words from model before core identity (Vision dumps).
    model_for_core = " ".join(
        token
        for token in significant_model_tokens(subject.model)
        if token not in _ACCESSORY_COLOR_TOKENS
        and token not in _DESCRIPTOR_MODEL_TOKENS
    ) or subject.model
    core = identity_core_tokens(model_for_core, color_tokens=color_tokens)
    tokens.extend(core)
    for code in extract_model_codes(subject.model):
        tokens.append(_fold(code))
    tokens.extend(color_tokens)
    # Case finish (dourado/gold) helps Tray AND when dial is already preto.
    case_tokens = preference_case_finish_tokens(interpretation)
    for token in case_tokens:
        if token in {"dourado", "gold", "golden", "ouro", "prata", "silver"}:
            tokens.append(token)
    # Keep movement when Vision asked Automatic — helps avoid GMT substitutes.
    if model_excludes_gmt(subject.model):
        tokens.append("automatico")
    # Drop ultra-generic fillers that drown AND matches.
    drop = {"relogio", "watch", "mm"} | _ACCESSORY_COLOR_TOKENS
    cleaned = [
        token
        for token in dict.fromkeys(tokens)
        if token and token not in drop and token not in _DESCRIPTOR_MODEL_TOKENS
    ]
    return tuple(cleaned)


def is_prospex_diver_ask(text: str | None) -> bool:
    """True when Vision/customer text is a Prospex diver dial (not a named line yet)."""
    folded = _fold(text)
    if not folded or "prospex" not in folded:
        return False
    return bool(
        re.search(r"\b(diver|divers|mergulho|200m|200)\b", folded)
        or "diver" in folded
    )


def is_promaster_sky_pilot_ask(text: str | None) -> bool:
    """Citizen Promaster aviation ana-digi / Eco-Drive often mislabeled Navihawk."""
    folded = _fold(text)
    if not folded:
        return False
    if any(label in folded for label in _PROMASTER_SKY_MISLABELS):
        return True
    if "sky" in folded and "pilot" in folded:
        return True
    if "jv2000" in folded:
        return True
    promasterish = "promaster" in folded or "citizen" in folded
    if not promasterish:
        return False
    digitalish = any(
        token in folded
        for token in (
            "eco drive",
            "ecodrive",
            "digital",
            "ana digi",
            "anadigi",
            "calendar",
            "slide rule",
            "regua",
        )
    )
    return digitalish


def commercial_model_aliases(
    model: str | None,
    *,
    brand: str | None = None,
) -> tuple[str, ...]:
    """Tray commercial names for dial-only Vision labels."""
    probe = " ".join(part for part in (brand, model) if part).strip() or (model or "")
    folded = _fold(model)
    brand_fold = _fold(brand)
    if is_promaster_sky_pilot_ask(probe) or is_promaster_sky_pilot_ask(model) or (
        brand_fold == "citizen" and is_promaster_sky_pilot_ask(f"{brand} {model}")
    ):
        if "sky" in folded and "pilot" in folded and "navihawk" not in folded:
            # Still probe JV2000 when Vision already said Sky Pilot.
            return tuple(
                alias
                for alias in _PROMASTER_SKY_PILOT_ALIASES
                if "jv2000" in _fold(alias)
            )
        return _PROMASTER_SKY_PILOT_ALIASES
    if not is_prospex_diver_ask(probe) and not is_prospex_diver_ask(model):
        return ()
    # Already a commercial line — do not expand to siblings.
    if "samurai" in folded or "turtle" in folded:
        return ()
    return _PROSPEX_DIVER_COMMERCIAL_ALIASES


def preference_feature_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Distinctive function tokens from preferences.attributes (photo Vision)."""
    tokens: list[str] = []
    for item in interpretation.preferences.attributes or []:
        folded = _fold(item)
        if not folded:
            continue
        if folded in {"pulseira_integrada", "acabamento_escovado"}:
            tokens.append(folded)
            continue
        if "integrad" in folded and "pulseira" in folded:
            tokens.append("pulseira_integrada")
        elif "escovad" in folded or "rajad" in folded:
            tokens.append("acabamento_escovado")
        elif "crono" in folded or "chrono" in folded:
            tokens.append("cronografo")
        elif "diver" in folded or "mergulho" in folded or "200m" in folded:
            tokens.append("mergulho")
        elif folded == "gmt":
            tokens.append("gmt")
    return tuple(dict.fromkeys(tokens))


def preference_gender_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Catalog search/ranking tokens for requested gender (soft, not AND-hard)."""
    from app.catalog.specs.preference_normalize import gender_search_aliases, preference_gender_label

    return gender_search_aliases(preference_gender_label(interpretation))


def product_matches_gender_tokens(
    product: dict[str, Any],
    gender_tokens: tuple[str, ...],
) -> bool:
    if not gender_tokens:
        return True
    text = _product_text(product)
    return any(token in text for token in gender_tokens)


def product_matches_feature_tokens(
    product: dict[str, Any],
    feature_tokens: tuple[str, ...],
) -> bool:
    if not feature_tokens:
        return True
    text = _product_text(product)
    for token in feature_tokens:
        aliases = _FEATURE_SEARCH_ALIASES.get(token, (token,))
        if not any(alias in text for alias in aliases):
            return False
    return True


def preference_case_finish_tokens(
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    """Soft case/bracelet finish cues (never AND-required alone)."""
    material = _fold(interpretation.preferences.material)
    if not material:
        return ()
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", material)
        if token and token not in _DESCRIPTOR_MODEL_TOKENS
    ]
    # Map common Vision finishes to catalog-ish tokens.
    expanded: list[str] = []
    for token in tokens:
        if token in {"prata", "silver", "aco", "steel", "inox"}:
            expanded.extend(["prata", "aco", "steel"])
        elif token in {"preto", "black", "ion", "pvd"}:
            expanded.extend(["preto", "black"])
        elif token in {"dourado", "gold", "golden", "ouro"}:
            expanded.extend(["dourado", "gold", "golden", "ouro"])
        else:
            expanded.append(token)
    return tuple(dict.fromkeys(expanded))


def product_matches_case_finish_tokens(
    product: dict[str, Any],
    case_tokens: tuple[str, ...],
) -> bool:
    if not case_tokens:
        return True
    text = _product_text(product)
    aliases: set[str] = set()
    for token in case_tokens:
        aliases |= set(expand_color_aliases(token))
        aliases.add(token)
    return any(alias in text for alias in aliases)


def product_matches_color_tokens(
    product: dict[str, Any],
    color_tokens: tuple[str, ...],
) -> bool:
    if not color_tokens:
        return True
    text = _product_text(product)
    for token in color_tokens:
        aliases = expand_color_aliases(token)
        if not any(alias in text for alias in aliases):
            return False
    return True


def product_conflicts_dial_color(
    product: dict[str, Any],
    color_tokens: tuple[str, ...],
) -> bool:
    """True when catalog title asserts a rival dial hue (rosa vs amora/verde)."""
    if not color_tokens:
        return False
    if product_matches_color_tokens(product, color_tokens):
        return False
    text = _product_text(product)
    allowed: set[str] = set()
    for token in color_tokens:
        allowed |= set(expand_color_aliases(token))
    rivals = _DIAL_COLOR_RIVAL_TOKENS - allowed
    # Word-boundary-ish: rival token appears as its own catalog hue word.
    return any(
        re.search(rf"\b{re.escape(rival)}\b", text)
        for rival in rivals
    )


def rank_products_for_dial_color(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = CUSTOMER_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Prefer dial-color matches; never surface rival hues when color is known."""
    color_tokens = preference_color_tokens(interpretation)
    if not color_tokens:
        return products[:limit]
    compatible = [
        product
        for product in products
        if product_matches_color_tokens(product, color_tokens)
        and not product_conflicts_dial_color(product, color_tokens)
    ]
    if compatible:
        from app.catalog.retrieval.scoring import score_catalog_candidates

        ranked = score_catalog_candidates(
            compatible,
            interpretation,
            require_color=True,
            allow_movement_mismatch=False,
            limit=limit,
        )
        return ranked or compatible[:limit]
    # No color lock in pool — do not invent amora/verde as "próximas" of rosa.
    return []


def model_excludes_gmt(model: str | None) -> bool:
    folded = _fold(model)
    if not folded:
        return False
    if "gmt" in folded:
        return False
    return "automatic" in folded or "automatico" in folded


def requests_automatic_movement(
    model: str | None,
    attributes: list[str] | None = None,
) -> bool:
    blob = _fold(
        " ".join(
            part
            for part in ((model or ""), *(attributes or ()))
            if part
        )
    )
    return "automatic" in blob or "automatico" in blob


def product_compatible_with_requested_movement(
    product: dict[str, Any],
    model: str | None,
    attributes: list[str] | None = None,
) -> bool:
    text = _product_text(product)
    wants_auto = requests_automatic_movement(model, attributes)
    if wants_auto:
        # Don't substitute GMT siblings for a plain Automatic ask.
        if "gmt" in text and "gmt" not in _fold(model):
            return False
        mechanical = (
            "mecanico" in text
            or "mechanical" in text
            or bool(re.search(r"\bh\s*mecan", text))
        )
        has_auto = "automatico" in text or "automatic" in text
        if mechanical and not has_auto:
            return False
    elif model_excludes_gmt(model):
        return "gmt" not in text
    return True


def extract_reference_code(text: str | None) -> str | None:
    match = _REFERENCE_CODE_RE.search(str(text or ""))
    return match.group(1).strip() if match else None


def extract_model_codes(text: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_MODEL_CODE_RE.findall(str(text or ""))))
