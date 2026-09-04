"""When this turn changes color/model/budget, Tray list search is the authority.

The durable catalog index may seed candidates. It must not skip the adaptor
fan-out, and a stale GET /products/{id} must not freeze the previous SKU.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import SalesInterpretation
from app.catalog.preference_normalize import extract_stated_color, message_states_color
from .discovery import _specific_product_lock, message_states_budget


_CASE_SIZE_RE = re.compile(r"\b\d{2}\s*mm\b", re.IGNORECASE)
_LIST_DENIAL_RE = re.compile(
    r"n[aã]o\s+s[aã]o|esses?\s+n[aã]o|n[aã]o\s+[eé]\s+dourado|"
    r"me\s+mandou\s+o\s+mesmo|n[aã]o\s+gostei|nenhum(a)?\s+dess|"
    r"n[aã]o\s+[eé]\s+(isso|esse|o\s+que)",
    re.IGNORECASE,
)
_TRAY_COLOR_PROPERTY = {
    "azul": "Azul",
    "preto": "Preto",
    "branco": "Branco",
    "verde": "Verde",
    "rosa": "Rosa",
    "dourado": "Dourado",
    "cinza": "Cinza",
    "vermelho": "Vermelho",
    "amarelo": "Amarelo",
    "laranja": "Laranja",
}


def is_list_denial(message_text: str | None) -> bool:
    return bool(_LIST_DENIAL_RE.search(str(message_text or "")))


def constraint_requires_tray_refresh(
    interpretation: SalesInterpretation | None,
    message_text: str | None,
) -> bool:
    """True when this turn must consult Tray instead of serving the index alone."""
    text = str(message_text or "")
    if message_states_color(text):
        return True
    if message_states_budget(text):
        return True
    if _CASE_SIZE_RE.search(text):
        return True
    if is_list_denial(text):
        return True
    if interpretation is not None and _specific_product_lock(interpretation):
        return True
    return False


def should_drop_contextual_resolve(
    *,
    interpretation: SalesInterpretation | None,
    message_text: str | None,
    purchase_close: bool = False,
) -> bool:
    """Color/model/budget change is a new list search, not GET of the prior SKU."""
    if purchase_close:
        return False
    if interpretation is not None:
        if interpretation.reference_type in {"current_product", "list_position"}:
            return False
        if interpretation.reference_position is not None:
            return False
        if interpretation.purchase_action:
            return False
    text = str(message_text or "")
    if not text.strip():
        return False
    if message_states_color(text):
        return True
    if message_states_budget(text):
        return True
    if _CASE_SIZE_RE.search(text):
        return True
    if is_list_denial(text):
        return True
    return False


def _presented_ids(commerce_state: Any | None) -> list[str]:
    presented = getattr(commerce_state, "last_presented_products", None) or []
    ids: list[str] = []
    for item in presented:
        product_id = getattr(item, "product_id", None)
        name = getattr(item, "name", None)
        if isinstance(item, dict):
            product_id = product_id or item.get("product_id") or item.get("id")
            name = name or item.get("name")
        if product_id:
            ids.append(str(product_id))
    return list(dict.fromkeys(ids))


def _existing_excluded(commerce_state: Any | None) -> list[str]:
    prefs = getattr(commerce_state, "active_preferences", None) or {}
    if not isinstance(prefs, dict):
        return []
    raw = prefs.get("excluded_product_ids") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def excluded_product_ids_for_turn(
    interpretation: SalesInterpretation | None,
    message_text: str | None,
    commerce_state: Any | None,
) -> list[str]:
    """Keep refused / model-mismatched shortlist IDs out of the next Tray pool."""
    ids = list(_existing_excluded(commerce_state))
    presented = getattr(commerce_state, "last_presented_products", None) or []
    if not presented:
        return list(dict.fromkeys(ids))

    text = str(message_text or "")
    if is_list_denial(text):
        ids.extend(_presented_ids(commerce_state))
        return list(dict.fromkeys(ids))

    from app.catalog.product_retrieval import required_model_tokens

    model = ""
    if interpretation is not None:
        model = str(interpretation.subject.model or "").strip()
    tokens = required_model_tokens(model) if model else ()
    if tokens:
        for item in presented:
            name = str(getattr(item, "name", None) or "")
            brand = str(getattr(item, "brand", None) or "")
            product_id = getattr(item, "product_id", None)
            if isinstance(item, dict):
                name = str(item.get("name") or name)
                brand = str(item.get("brand") or brand)
                product_id = product_id or item.get("product_id") or item.get("id")
            hay = f"{name} {brand}".casefold()
            if product_id and not all(token in hay for token in tokens):
                ids.append(str(product_id))

    color = extract_stated_color(text)
    if color:
        from app.catalog.product_retrieval import product_matches_color_tokens

        color_tokens = (color,)
        for item in presented:
            product_id = getattr(item, "product_id", None)
            payload = item if isinstance(item, dict) else None
            if payload is None:
                payload = {
                    "name": getattr(item, "name", None),
                    "brand": getattr(item, "brand", None),
                    "id": getattr(item, "product_id", None),
                }
            if isinstance(item, dict):
                product_id = product_id or item.get("product_id") or item.get("id")
            if product_id and not product_matches_color_tokens(payload, color_tokens):
                ids.append(str(product_id))

    return list(dict.fromkeys(item for item in ids if item))


def tray_list_query_extras(interpretation: SalesInterpretation | None) -> dict[str, Any]:
    """Adaptor-supported GET /internal/products filters (never a substitute for local hard_filter)."""
    extras: dict[str, Any] = {}
    if interpretation is None:
        return extras
    budget = interpretation.preferences.budget_max
    if budget is not None:
        try:
            ceiling = max(0, int(float(budget)))
            extras["current_price_range"] = f"0,{ceiling}"
        except (TypeError, ValueError):
            pass
    color = str(interpretation.preferences.color or "").strip().casefold() or None
    tray_color = _TRAY_COLOR_PROPERTY.get(color or "")
    if tray_color:
        extras["property_name"] = "Cor"
        extras["property_value"] = tray_color
    attrs = " ".join(str(item) for item in (interpretation.preferences.attributes or []))
    if any(token in attrs.casefold() for token in ("pronta", "qual:urgency:rush")):
        extras["available"] = True
    return extras
