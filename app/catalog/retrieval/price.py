from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.catalog.retrieval.types import CommercialPriceResolution

def _decimal_money_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("value", "amount"):
            if key in value:
                return _decimal_money_value(value.get(key))
        return None
    try:
        if isinstance(value, str):
            normalized = value.replace("R$", "").replace("\xa0", " ").strip()
            normalized = normalized.replace(" ", "")
            if "," in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            return Decimal(normalized)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


money_decimal = _decimal_money_value


def resolve_commercial_price(
    product: dict[str, Any],
    *,
    require_positive: bool = False,
) -> CommercialPriceResolution:
    first_present_source: str | None = None
    for key in ("current_price", "promotional_price", "price"):
        if key not in product or product.get(key) is None:
            continue
        first_present_source = first_present_source or key
        amount = _decimal_money_value(product.get(key))
        if require_positive and (amount is None or amount <= Decimal("0")):
            continue
        return CommercialPriceResolution(amount=amount, source=key)
    return CommercialPriceResolution(amount=None, source=first_present_source)


def effective_price(product: dict[str, Any]) -> float | None:
    resolved = resolve_commercial_price(product)
    return float(resolved.amount) if resolved.amount is not None else None
