"""Turn contract: what this WhatsApp turn must honor.

Built from two inbound views (this message vs memory/state). The message wins
on conflict; memory-only fields are marked stale so retrieval and copy cannot
treat them as if the customer just said them.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..models import SalesInterpretation
from app.catalog.preference_normalize import (
    _extract_budget_max,
    extract_stated_color,
    extract_stated_gender,
    extract_stated_style,
    message_states_color,
    message_states_gender,
    message_states_style,
    preference_gender_label,
)
from .discovery import (
    _mentioned_watch_brands,
    _specific_product_lock,
    is_open_catalog_browse_request,
    message_states_budget,
    message_states_occasion,
)


def _fold_identity(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return " ".join(text.split())


def _infer_identity_from_presented(
    presented: Any,
) -> tuple[str | None, str | None]:
    """Recover brand/model from the live shortlist (MK2 names, shared brand)."""
    if not presented:
        return None, None
    brands: list[str] = []
    names: list[str] = []
    for item in presented:
        if item is None:
            continue
        brand = getattr(item, "brand", None)
        name = getattr(item, "name", None)
        if isinstance(item, dict):
            brand = brand or item.get("brand")
            name = name or item.get("name")
        if brand:
            brands.append(str(brand).strip())
        if name:
            names.append(str(name).strip())
    common_brand = None
    if brands:
        folded = [_fold_identity(item) for item in brands]
        if folded and all(item == folded[0] for item in folded):
            common_brand = brands[0]
    blob = " ".join(part for part in names if part)
    if not blob:
        return common_brand, None
    from ..models import ProductPreferences, ProductSubject
    from app.catalog.preference_normalize import repair_specific_model_tokens

    subject = ProductSubject(brand=common_brand)
    prefs = ProductPreferences()
    repair_specific_model_tokens(
        subject,
        prefs,
        message_text=blob,
        context_text=blob,
    )
    return subject.brand or common_brand, subject.model


def locked_identity_from_state(
    commerce_state: Any | None,
) -> tuple[str | None, str | None]:
    """Durable line chosen this session, else infer from presented SKUs."""
    prefs: dict[str, Any] = {}
    if commerce_state is not None:
        raw = getattr(commerce_state, "active_preferences", None)
        if isinstance(raw, dict):
            prefs = raw
    locked = prefs.get("locked_identity")
    brand = None
    model = None
    if isinstance(locked, dict):
        brand = str(locked.get("brand") or "").strip() or None
        model = str(locked.get("model") or "").strip() or None
    if model:
        return brand, model
    presented = getattr(commerce_state, "last_presented_products", None) or []
    return _infer_identity_from_presented(presented)


def next_locked_identity(
    interpretation: SalesInterpretation | None,
    commerce_state: Any | None,
    message_text: str | None,
) -> dict[str, str] | None:
    """Persist the chosen line unless the customer opened a new browse."""
    try:
        from .dialogue_phase import message_resets_dialogue_to_discovery

        if message_resets_dialogue_to_discovery(message_text, interpretation):
            return None
    except Exception:
        pass
    if interpretation is not None and _specific_product_lock(interpretation):
        payload: dict[str, str] = {}
        if interpretation.subject.brand:
            payload["brand"] = str(interpretation.subject.brand).strip()
        if interpretation.subject.model:
            payload["model"] = str(interpretation.subject.model).strip()
        if interpretation.subject.reference:
            payload["reference"] = str(interpretation.subject.reference).strip()
        return payload or None
    brand, model = locked_identity_from_state(commerce_state)
    if not model:
        return None
    payload = {"model": model}
    if brand:
        payload["brand"] = brand
    return payload


_GREETING_RE = re.compile(
    r"^\s*(oi|ol[aá]|bom dia|boa tarde|boa noite|hello|hi)[!.,\s]*$",
    re.IGNORECASE,
)
_RANGE_ASK_RE = re.compile(
    r"\b(nessa faixa|faixa de pre[cç]o|faixa que pedi|or[cç]amento)\b",
    re.IGNORECASE,
)
_OCCASION_CLAIM_RE = re.compile(
    r"\bpara (o )?trabalho\b|\bno trabalho\b|\bpara esporte\b|\bdia a dia\b",
    re.IGNORECASE,
)
_CHECKOUT_CLAIM_RE = re.compile(
    r"\b("
    r"carrinho|checkout|"
    r"link de pagamento|link do pagamento|"
    r"copia e cola|c[oó]digo pix|pix copia|"
    r"qr\s*code|"
    r"forma de pagamento|"
    r"fechar por aqui|"
    r"continuar pelo site"
    r")\b|"
    r"https?://\S*(?:pay|checkout|pagamento)",
    re.IGNORECASE,
)


class InboundView(BaseModel):
    source: str
    brand: str | None = None
    model: str | None = None
    budget_max: float | None = None
    occasion: str | None = None
    color: str | None = None
    gender: str | None = None
    style: str | None = None
    is_greeting: bool = False
    asks_price_range: bool = False
    commerce_browse: bool = False
    live_shortlist: bool = False
    live_checkout: bool = False


class TurnContract(BaseModel):
    asked_text: str = ""
    brand: str | None = None
    model: str | None = None
    budget_max: float | None = None
    budget_from_this_message: bool = False
    occasion_from_this_message: bool = False
    asks_price_range: bool = False
    stale_fields: list[str] = Field(default_factory=list)
    sku_lock: bool = False
    purchase_close: bool = False
    live_shortlist: bool = False
    live_checkout: bool = False
    color: str | None = None
    gender: str | None = None
    style: str | None = None
    color_from_this_message: bool = False
    gender_from_this_message: bool = False
    style_from_this_message: bool = False
    must_not_re_greet: bool = False
    must_not_claim_stale_occasion: bool = False
    must_not_claim_stale_checkout: bool = False
    hard_codes: list[str] = Field(default_factory=list)


def inbound_from_message(
    message_text: str | None,
    interpretation: SalesInterpretation | None,
) -> InboundView:
    text = str(message_text or "").strip()
    brands = _mentioned_watch_brands(text)
    brand = brands[0] if brands else (
        interpretation.subject.brand if interpretation is not None else None
    )
    budget = _extract_budget_max(text) if text else None
    if budget is None and interpretation is not None and message_states_budget(text):
        budget = interpretation.preferences.budget_max
    occasion = None
    if message_states_occasion(text) and interpretation is not None:
        occasion = interpretation.preferences.occasion
    color = extract_stated_color(text)
    if (
        color is None
        and interpretation is not None
        and interpretation.preferences.color
        and message_states_color(text)
    ):
        color = interpretation.preferences.color
    gender = extract_stated_gender(text)
    if gender is None and interpretation is not None and message_states_gender(text):
        gender = preference_gender_label(interpretation)
    style = extract_stated_style(text)
    if (
        style is None
        and interpretation is not None
        and interpretation.preferences.style
        and message_states_style(text)
    ):
        style = interpretation.preferences.style
    return InboundView(
        source="message",
        brand=brand,
        model=(
            interpretation.subject.model
            if interpretation is not None
            else None
        ),
        budget_max=budget,
        occasion=occasion,
        color=color,
        gender=gender,
        style=style,
        is_greeting=bool(_GREETING_RE.match(text)),
        asks_price_range=bool(_RANGE_ASK_RE.search(text)),
        commerce_browse=bool(
            interpretation is not None
            and is_open_catalog_browse_request(text, interpretation)
        ),
    )


def _packed_pref(raw: Any) -> str | None:
    if isinstance(raw, dict):
        value = raw.get("value") or raw.get("color") or raw.get("style") or raw.get("gender")
        return str(value).strip() if value else None
    if raw in (None, ""):
        return None
    return str(raw).strip() or None


def inbound_from_memory(
    interpretation: SalesInterpretation | None,
    commerce_state: Any | None,
) -> InboundView:
    prefs: dict[str, Any] = {}
    if commerce_state is not None:
        raw = getattr(commerce_state, "active_preferences", None)
        if isinstance(raw, dict):
            prefs = raw
    budget = None
    if interpretation is not None:
        budget = interpretation.preferences.budget_max
    if budget is None:
        packed = prefs.get("budget") if isinstance(prefs.get("budget"), dict) else {}
        raw = packed.get("max") if isinstance(packed, dict) else prefs.get("budget_max")
        try:
            budget = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            budget = None
    brand = None
    model = None
    if interpretation is not None:
        brand = interpretation.subject.brand
        model = interpretation.subject.model
    locked_brand, locked_model = locked_identity_from_state(commerce_state)
    if not brand:
        brand = locked_brand
    if not model:
        model = locked_model
    occasion = None
    if interpretation is not None:
        occasion = interpretation.preferences.occasion
    if not occasion:
        occ = prefs.get("occasion")
        occasion = occ.get("value") if isinstance(occ, dict) else occ
    color = None
    gender = None
    style = None
    if interpretation is not None:
        color = interpretation.preferences.color
        style = interpretation.preferences.style
        gender = preference_gender_label(interpretation)
    if not color:
        color = _packed_pref(prefs.get("color"))
    if not style:
        style = _packed_pref(prefs.get("style"))
    if not gender:
        gender = _packed_pref(prefs.get("gender"))
    presented = getattr(commerce_state, "last_presented_products", None) or []
    live_shortlist = bool(presented)
    phase = getattr(commerce_state, "dialogue_phase", None)
    live_checkout = bool(
        getattr(commerce_state, "active_product", None)
        or getattr(commerce_state, "cart_id", None)
        or getattr(commerce_state, "cart_session_id", None)
        or getattr(commerce_state, "order_id", None)
        or getattr(commerce_state, "order_lookup_id", None)
        or getattr(commerce_state, "pending_action", None)
        or phase in {"shortlist", "buy", "checkout"}
    )
    return InboundView(
        source="memory",
        brand=brand,
        model=str(model).strip() if model else None,
        budget_max=budget,
        occasion=str(occasion).strip() if occasion else None,
        color=str(color).strip() if color else None,
        gender=str(gender).strip() if gender else None,
        style=str(style).strip() if style else None,
        live_shortlist=live_shortlist,
        live_checkout=live_checkout,
    )


def merge_inbound_views(
    *,
    message_view: InboundView,
    memory_view: InboundView,
    message_text: str | None,
    interpretation: SalesInterpretation | None,
) -> TurnContract:
    """Message wins. Memory-only occasion/style on a fresh browse is stale."""
    stale: list[str] = []
    budget = message_view.budget_max
    budget_from_message = message_view.budget_max is not None or message_view.asks_price_range
    if budget is None and (budget_from_message or not message_view.commerce_browse):
        budget = memory_view.budget_max
    elif message_view.commerce_browse and not budget_from_message:
        if memory_view.budget_max is not None:
            stale.append("budget")
        budget = None

    if message_view.asks_price_range and budget is None:
        budget = memory_view.budget_max

    occasion_from_message = bool(message_view.occasion) or message_states_occasion(
        message_text
    )
    if memory_view.occasion and not occasion_from_message:
        if message_view.commerce_browse or message_view.asks_price_range:
            stale.append("occasion")

    def _merge_pref(
        *,
        message_value: str | None,
        memory_value: str | None,
        stated: bool,
        field: str,
    ) -> str | None:
        if stated:
            return message_value
        if message_view.commerce_browse:
            if memory_value:
                stale.append(field)
            return None
        return message_value or memory_value

    color_from_message = bool(message_view.color) or message_states_color(message_text)
    gender_from_message = bool(message_view.gender) or message_states_gender(message_text)
    style_from_message = bool(message_view.style) or message_states_style(message_text)
    color = _merge_pref(
        message_value=message_view.color,
        memory_value=memory_view.color,
        stated=color_from_message,
        field="color",
    )
    gender = _merge_pref(
        message_value=message_view.gender,
        memory_value=memory_view.gender,
        stated=gender_from_message,
        field="gender",
    )
    style = _merge_pref(
        message_value=message_view.style,
        memory_value=memory_view.style,
        stated=style_from_message,
        field="style",
    )

    brand = message_view.brand or memory_view.brand
    model = message_view.model
    if message_view.commerce_browse:
        model = message_view.model
    else:
        model = message_view.model or memory_view.model
    codes = ["dual_inbound_merged"]
    if stale:
        codes.append("stale_memory")
    if budget is not None:
        codes.append("hard_budget")
    if brand:
        codes.append("brand_lock")
    if model:
        codes.append("model_lock")
    if message_view.asks_price_range:
        codes.append("honor_stated_range")
    if color:
        codes.append("color_lock")
    if gender:
        codes.append("gender_lock")
    if style:
        codes.append("style_lock")

    sku_lock = False
    if interpretation is not None:
        sku_lock = bool(
            interpretation.subject.reference or interpretation.subject.ean
        )
        if not sku_lock:
            sku_lock = _specific_product_lock(interpretation)
        if sku_lock:
            codes.append("sku_lock")
    if model and not sku_lock and not message_view.commerce_browse:
        sku_lock = True
        codes.append("sku_lock")
    purchase_close = False
    if memory_view.live_shortlist:
        from .purchase_selection import (
            is_bare_purchase_closing,
            parse_list_position_selection,
        )

        purchase_close = bool(
            parse_list_position_selection(message_text)
            or is_bare_purchase_closing(message_text)
        )
        if purchase_close:
            sku_lock = True
            codes.append("sku_lock")

    live_shortlist = memory_view.live_shortlist
    if live_shortlist:
        codes.append("live_shortlist")

    live_checkout = memory_view.live_checkout
    browse_reset = False
    try:
        from .dialogue_phase import message_resets_dialogue_to_discovery

        browse_reset = message_resets_dialogue_to_discovery(
            message_text, interpretation
        )
    except Exception:
        browse_reset = message_view.commerce_browse
    if browse_reset and not (
        interpretation is not None and _specific_product_lock(interpretation)
    ):
        model = message_view.model
        if interpretation is None or not (
            interpretation.subject.reference or interpretation.subject.ean
        ):
            sku_lock = bool(purchase_close)
    if (
        live_checkout
        and (
            browse_reset
            or message_view.commerce_browse
            or message_view.asks_price_range
        )
        and not purchase_close
        and not message_view.is_greeting
    ):
        stale.append("checkout")
        live_checkout = False
    if live_checkout:
        codes.append("checkout_lock")
    if (
        interpretation is not None
        and interpretation.purchase_action in {
            "create_cart",
            "checkout_question",
            "show_cart_link",
        }
        and not browse_reset
        and not message_view.commerce_browse
    ):
        purchase_close = True
        sku_lock = True
        codes.append("sku_lock")

    must_not_re_greet = bool(
        message_view.commerce_browse
        or message_view.asks_price_range
        or live_shortlist
        or live_checkout
        or sku_lock
    )

    return TurnContract(
        asked_text=str(message_text or "").strip(),
        brand=brand,
        model=model,
        budget_max=budget,
        budget_from_this_message=budget_from_message,
        occasion_from_this_message=occasion_from_message,
        asks_price_range=message_view.asks_price_range,
        stale_fields=list(dict.fromkeys(stale)),
        sku_lock=sku_lock,
        purchase_close=purchase_close,
        live_shortlist=live_shortlist,
        live_checkout=live_checkout,
        color=color,
        gender=gender,
        style=style,
        color_from_this_message=color_from_message,
        gender_from_this_message=gender_from_message,
        style_from_this_message=style_from_message,
        must_not_re_greet=must_not_re_greet,
        must_not_claim_stale_occasion="occasion" in stale,
        must_not_claim_stale_checkout="checkout" in stale,
        hard_codes=codes,
    )


def reply_claims_occasion(reply_text: str | None) -> bool:
    return bool(_OCCASION_CLAIM_RE.search(str(reply_text or "")))


def reply_claims_checkout(reply_text: str | None) -> bool:
    return bool(_CHECKOUT_CLAIM_RE.search(str(reply_text or "")))
