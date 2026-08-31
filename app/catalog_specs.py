"""Extract structured watch specs from Tray titles / payloads for the catalog index.

Used at index write and at retrieval time so diver / case-size ranking does not
depend solely on free-text LLM judgment (Certina DS-7 vs DS Action, 25/08).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def _fold(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_SIZE_RE = re.compile(r"\b([2-5]\d)\s*mm\b", re.IGNORECASE)
_WR_RE = re.compile(
    r"\b(\d{2,4})\s*(?:m|metros?)\b"
    r"|\b(?:wr|atm|bar)\s*[:=]?\s*(\d{1,3})\b"
    r"|\b(\d{1,3})\s*(?:atm|bar)\b",
    re.IGNORECASE,
)

# Lines that are true dive watches even when title omits "200m".
_TRUE_DIVER_LINE_RE = re.compile(
    r"\b("
    r"aquascaphe|ds\s*action|seastar|prospex|samurai|turtle|"
    r"diver'?s?|mergulho|sub\s*300|professional\s*300|"
    r"pelagos|black\s*bay|superocean|aquis|planet\s*ocean"
    r")\b",
    re.IGNORECASE,
)

# Soft/dress lines often mis-sold as divers when the customer asks for caixa menor.
_FALSE_DIVER_LINE_RE = re.compile(
    r"\b("
    r"ds-?7|dress|casual|prestige|gentleman|everytime"
    r")\b",
    re.IGNORECASE,
)


def product_spec_blob(product: dict[str, Any] | None) -> str:
    """Flatten Tray-ish product fields for regex extraction."""
    if not isinstance(product, dict):
        return ""
    chunks: list[str] = []
    for key in (
        "name",
        "title",
        "title_normalized",
        "description",
        "model",
        "brand",
        "category",
        "category_name",
        "properties",
        "attributes",
        "ProductDescription",
        "metatag",
        "meta_description",
    ):
        value = product.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            chunks.append(str(value))
        else:
            chunks.append(str(value))
    # Nested Tray ProductDescription / properties sometimes carry WR.
    raw = product.get("raw") if isinstance(product.get("raw"), dict) else None
    if raw:
        for key in ("description", "Description", "properties", "Properties"):
            if raw.get(key) is not None:
                chunks.append(str(raw.get(key)))
    return _fold(" ".join(chunks))


def extract_case_size_mm(product: dict[str, Any] | str | None) -> str | None:
    """Return case diameter as digits string (e.g. '39'), or None."""
    if isinstance(product, dict):
        explicit = product.get("case_size") or product.get("case_size_mm")
        if explicit is not None and str(explicit).strip():
            match = re.search(r"(\d{2})", str(explicit))
            if match:
                return match.group(1)
        blob = product_spec_blob(product)
    else:
        blob = _fold(product)
    match = _SIZE_RE.search(blob)
    if not match:
        return None
    size = int(match.group(1))
    # Watch cases are typically 28–55 mm; reject noise like years/refs.
    if 28 <= size <= 55:
        return str(size)
    return None


def extract_water_resistance_m(product: dict[str, Any] | str | None) -> int | None:
    """Return water resistance in meters when detectable."""
    if isinstance(product, dict):
        for key in (
            "water_resistance_m",
            "water_resistance",
            "water_resist",
            "wr_m",
        ):
            raw = product.get(key)
            if raw is None or raw == "":
                continue
            try:
                value = int(float(str(raw).replace("m", "").strip()))
                if 10 <= value <= 2000:
                    return value
            except (TypeError, ValueError):
                pass
        blob = product_spec_blob(product)
    else:
        blob = _fold(product)

    best: int | None = None
    for match in _WR_RE.finditer(blob):
        groups = [g for g in match.groups() if g]
        if not groups:
            continue
        try:
            value = int(groups[0])
        except ValueError:
            continue
        # ATM/bar ≈ 10m each when the unit was atm/bar.
        span = match.group(0).lower()
        if "atm" in span or "bar" in span:
            value *= 10
        if value < 10 or value > 2000:
            continue
        if best is None or value > best:
            best = value
    return best


def is_true_diver_product(product: dict[str, Any] | None) -> bool:
    """Strong positive evidence of a dive watch."""
    blob = product_spec_blob(product) if isinstance(product, dict) else _fold(product)
    wr = extract_water_resistance_m(product)
    if wr is not None and wr >= 200:
        return True
    if _TRUE_DIVER_LINE_RE.search(blob):
        return True
    return False


def is_false_diver_product(product: dict[str, Any] | None) -> bool:
    """Dress / 100m lines that must not win a diver ask (DS-7 pattern)."""
    if is_true_diver_product(product):
        return False
    blob = product_spec_blob(product) if isinstance(product, dict) else _fold(product)
    wr = extract_water_resistance_m(product)
    if wr is not None and wr <= 100:
        return True
    if _FALSE_DIVER_LINE_RE.search(blob):
        return True
    return False


def extract_material(product: dict[str, Any] | str | None) -> str | None:
    """Harvest case material from Tray field or title/description (titânio/aço)."""
    if isinstance(product, dict):
        explicit = product.get("material")
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        blob = product_spec_blob(product)
    else:
        blob = _fold(product)
    if re.search(r"\b(titanio|titanium|ti\s*case)\b", blob):
        return "titânio"
    if re.search(r"\b(ceramic|ceramica|cerâmica)\b", blob):
        return "cerâmica"
    if re.search(r"\b(ouro|gold|rose\s*gold|ouro\s*rosa)\b", blob):
        return "ouro"
    if re.search(r"\b(aco|aço|steel|inox|stainless)\b", blob):
        return "aço"
    return None


def reference_from_store_url(url: str | None) -> str | None:
    """Pull Certina-style refs from storefront slug (c032-807-44-081-00)."""
    from urllib.parse import urlparse

    raw = str(url or "").strip()
    if not raw:
        return None
    slug = urlparse(raw).path.rstrip("/").split("/")[-1]
    certina_match = re.search(
        r"(c\d{3}[-\.]\d{3}[-\.]\d{2}[-\.]\d{3}[-\.]\d{2})",
        slug,
        re.IGNORECASE,
    )
    if certina_match:
        return certina_match.group(1).replace("-", ".").upper()
    dotted = slug.replace("-", ".")
    ref_match = re.search(
        r"\b("
        r"[A-Z]{1,4}\d{2,}[A-Z0-9]*(?:-[A-Z0-9]{1,})+"
        r"|[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,}"
        r"|[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){2,}"
        r")\b",
        dotted,
        re.IGNORECASE,
    )
    if ref_match:
        return ref_match.group(1)
    return None


def interpretation_wants_diver(interpretation: Any) -> bool:
    prefs = getattr(interpretation, "preferences", None)
    subject = getattr(interpretation, "subject", None)
    parts = [
        getattr(prefs, "style", None) if prefs else None,
        getattr(prefs, "occasion", None) if prefs else None,
        getattr(subject, "product_type", None) if subject else None,
        getattr(subject, "model", None) if subject else None,
    ]
    if prefs is not None:
        parts.extend(list(getattr(prefs, "attributes", None) or []))
    blob = _fold(" ".join(str(p) for p in parts if p))
    return any(
        token in blob
        for token in ("mergulho", "diver", "divers", "aquascaphe", "dive")
    )


_CASE_RANGE_RE = re.compile(
    r"(?:entre|de)\s*(\d{2})\s*(?:a|ate|at[eé]|[-–])\s*(\d{2})\s*mm"
    r"|(\d{2})\s*(?:a|ate|at[eé]|[-–])\s*(\d{2})\s*mm",
    re.IGNORECASE,
)
_SMALL_WRIST_RE = re.compile(
    r"\b(pulso\s*(?:pequeno|menor|fin[oa])|caixa\s*menor|tamanho\s*menor)\b",
    re.IGNORECASE,
)
_KNOWN_WATCH_BRANDS: tuple[str, ...] = (
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

_OTHER_BRANDS_RE = re.compile(
    r"\b("
    r"outras?\s+marcas?|"
    r"outras?\s+op(?:ç|c)(?:õ|o)es?\s+(?:de\s+)?marcas?|"
    r"de\s+outras?\s+marcas?|"
    r"outra\s+marca|"
    r"qualquer\s+marca|"
    r"sem\s+prefer[eê]ncia\s+de\s+marca|"
    r"n[aã]o\s+precisa\s+ser|"
    r"n[aã]o\s+s[oó]\s+(?:da|de)?\s*"
    + "(?:"
    + "|".join(re.escape(brand) for brand in _KNOWN_WATCH_BRANDS)
    + r")"
    r")\b",
    re.IGNORECASE,
)
_CHRONOGRAPH_RE = re.compile(
    r"\b(cron[oó]grafo|cronografo|chronograph|chrono|crono)\b",
    re.IGNORECASE,
)
_EXCLUDE_BRAND_ATTR_PREFIX = "exclude_brand:"


def extract_case_size_range_from_text(text: str | None) -> tuple[int, int] | None:
    """Parse explicit mm ranges such as '36 até 38mm' or 'entre 36 e 38 mm'."""
    blob = _fold(text)
    if not blob:
        return None
    match = _CASE_RANGE_RE.search(blob)
    if match:
        low = int(match.group(1) or match.group(3))
        high = int(match.group(2) or match.group(4))
        if low > high:
            low, high = high, low
        if 28 <= low <= 55 and 28 <= high <= 55:
            return low, high
    singles = [int(item) for item in re.findall(r"\b(3[0-9]|4[0-5])\s*mm\b", blob)]
    if len(singles) >= 2:
        low, high = min(singles[:2]), max(singles[:2])
        if 28 <= low <= 55 and 28 <= high <= 55:
            return low, high
    if len(singles) == 1 and 28 <= singles[0] <= 55:
        return singles[0], singles[0]
    return None


def message_requests_other_brands(text: str | None) -> bool:
    """True when the customer opens the search beyond the current sticky brand."""
    raw = str(text or "")
    if not raw.strip():
        return False
    if extract_rejected_brands_from_text(raw):
        return True
    return bool(_OTHER_BRANDS_RE.search(raw))


def message_wants_chronograph(text: str | None) -> bool:
    return bool(_CHRONOGRAPH_RE.search(str(text or "")))


def extract_rejected_brands_from_text(text: str | None) -> list[str]:
    """Brands the customer explicitly does not want (e.g. 'não precisa ser certina')."""
    blob = _fold(text)
    if not blob:
        return []
    rejected: list[str] = []
    for brand in _KNOWN_WATCH_BRANDS:
        if brand not in blob:
            continue
        patterns = (
            rf"\bnao\s+precisa\s+(?:ser|ser\s+da|ser\s+de)\s+{re.escape(brand)}\b",
            rf"\bnao\s+quero\s+(?:da|de|o|a|um|uma|chrono|crono|cronografo|cronógrafo)?\s*{re.escape(brand)}\b",
            rf"\bnao\s+quero\s+.*\b{re.escape(brand)}\b",
            rf"\bnao\s+so\s+(?:da|de)?\s*{re.escape(brand)}\b",
            rf"\bfora\s+da\s+{re.escape(brand)}\b",
            rf"\b(?:alem|além)\s+da\s+{re.escape(brand)}\b",
            rf"\bsem\s+(?:ser\s+)?{re.escape(brand)}\b",
        )
        if any(re.search(pattern, blob) for pattern in patterns):
            label = "TAG Heuer" if brand == "tag heuer" else brand.title()
            if label not in rejected:
                rejected.append(label)
    return rejected


def excluded_brands_from_interpretation(interpretation: Any) -> list[str]:
    """Collect exclude_brand:* attributes already attached to the turn."""
    prefs = getattr(interpretation, "preferences", None)
    attrs = list(getattr(prefs, "attributes", None) or []) if prefs is not None else []
    excluded: list[str] = []
    for item in attrs:
        raw = str(item or "")
        if not raw.lower().startswith(_EXCLUDE_BRAND_ATTR_PREFIX):
            continue
        brand = raw.split(":", 1)[1].strip()
        if brand and brand not in excluded:
            excluded.append(brand)
    return excluded


def apply_brand_unlock_to_interpretation(
    interpretation: Any,
    *,
    message_text: str | None = None,
) -> list[str]:
    """Clear sticky brand and record exclusions. Returns rejected brand labels."""
    prefs = getattr(interpretation, "preferences", None)
    subject = getattr(interpretation, "subject", None)
    if prefs is None or subject is None:
        return []
    rejected = extract_rejected_brands_from_text(message_text)
    unlock = message_requests_other_brands(message_text)
    if not unlock and not rejected:
        return []
    attrs = list(prefs.attributes or [])
    for brand in rejected:
        label = f"{_EXCLUDE_BRAND_ATTR_PREFIX}{brand}"
        if label not in attrs:
            attrs.append(label)
        current = _fold(getattr(subject, "brand", None))
        if current and current == _fold(brand):
            subject.brand = None
    if unlock:
        subject.brand = None
        explicit = list(prefs.explicit_no_preferences or [])
        if "brand" not in explicit:
            prefs.explicit_no_preferences = explicit + ["brand"]
    prefs.attributes = attrs
    return rejected


def product_matches_excluded_brand(
    product: dict[str, Any] | None,
    excluded_brands: list[str] | tuple[str, ...] | None,
) -> bool:
    if not product or not excluded_brands:
        return False
    candidate = _fold(product.get("brand"))
    blob = product_spec_blob(product) if isinstance(product, dict) else ""
    for brand in excluded_brands:
        folded = _fold(brand)
        if not folded:
            continue
        if candidate and (candidate == folded or folded in candidate or candidate in folded):
            return True
        if folded and folded in blob:
            return True
    return False


def product_case_size_mm(product: dict[str, Any] | None) -> int | None:
    raw = extract_case_size_mm(product)
    if raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    if 28 <= size <= 55:
        return size
    return None


def product_matches_case_size_range(
    product: dict[str, Any] | None,
    min_mm: int,
    max_mm: int,
) -> bool:
    size = product_case_size_mm(product)
    if size is None:
        return False
    return min_mm <= size <= max_mm


def interpretation_case_size_range(
    interpretation: Any,
    *,
    message_text: str | None = None,
    context_text: str | None = None,
) -> tuple[int, int] | None:
    """Return requested case-size window when the customer gave an explicit mm range."""
    texts: list[str] = []
    if message_text:
        texts.append(message_text)
    if context_text:
        texts.append(context_text)
    prefs = getattr(interpretation, "preferences", None)
    if prefs is not None:
        for item in getattr(prefs, "attributes", None) or []:
            if not item:
                continue
            texts.append(str(item))
            if str(item).startswith("case_size:"):
                parsed = extract_case_size_range_from_text(str(item).replace("case_size:", ""))
                if parsed:
                    return parsed
    try:
        from .turn_understanding import get_turn_understanding

        turn = get_turn_understanding(interpretation)
        if turn is not None:
            for value in (
                turn.hard_constraints.case_size,
                turn.entities.case_size,
                turn.soft_preferences.case_size,
            ):
                if value:
                    texts.append(str(value))
    except Exception:
        pass
    for text in texts:
        parsed = extract_case_size_range_from_text(text)
        if parsed:
            return parsed
    blob = _fold(" ".join(texts))
    if _SMALL_WRIST_RE.search(blob) and not extract_case_size_range_from_text(blob):
        return 36, 38
    return None


def interpretation_wants_small_case(interpretation: Any) -> bool:
    if interpretation_case_size_range(interpretation):
        return True
    prefs = getattr(interpretation, "preferences", None)
    parts: list[Any] = []
    if prefs is not None:
        parts.extend(
            [
                getattr(prefs, "style", None),
                getattr(prefs, "occasion", None),
                *list(getattr(prefs, "attributes", None) or []),
            ]
        )
    blob = _fold(" ".join(str(p) for p in parts if p))
    if _SMALL_WRIST_RE.search(blob):
        return True
    if any(
        token in blob
        for token in ("caixa menor", "menor", "compacto", "39mm", "37mm", "38mm")
    ):
        return True
    return bool(re.search(r"\b(3[5-9]|40)\s*mm\b", blob))
