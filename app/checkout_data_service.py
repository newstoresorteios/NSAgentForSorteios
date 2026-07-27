from __future__ import annotations

import re
from typing import Any

from .commerce_context import (
    CHECKOUT_REQUIRED_FIELDS,
    CommerceConversationState,
    checkout_fields_view,
    checkout_missing_fields,
)
from .models import AgentResult


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CUSTOMER_FIELDS = {"name", "cpf", "email", "phone", "rg", "gender"}
_ADDRESS_FIELDS = {
    "address", "zipcode", "zip_code", "number", "complement",
    "neighborhood", "city", "state",
}


def normalize_zipcode(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 8 else None


def _normalize_cpf(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11 or digits == digits[0] * 11:
        return None
    for size in (9, 10):
        total = sum(int(digit) * weight for digit, weight in zip(
            digits[:size], range(size + 1, 1, -1)
        ))
        check = (total * 10 % 11) % 10
        if check != int(digits[size]):
            return None
    return digits


def _normalize_phone(value: Any) -> str | None:
    """Canoniza para o contrato do TrayAdapter: 10 ou 11 dígitos, sem DDI."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return None
    return digits


def _clean_text(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    return text or None


def update_checkout_data(
    state: CommerceConversationState,
    updates: dict[str, Any],
) -> AgentResult:
    allowed = _CUSTOMER_FIELDS | _ADDRESS_FIELDS
    unknown = sorted(set(updates) - allowed)
    errors: dict[str, str] = {
        field: "field_not_allowed" for field in unknown
    }
    normalized: dict[str, str | None] = {}
    for field, value in updates.items():
        if field not in allowed or value is None:
            continue
        canonical = "zipcode" if field == "zip_code" else field
        if canonical == "cpf":
            parsed = _normalize_cpf(value)
            if parsed is None:
                errors[canonical] = "invalid_cpf"
            else:
                normalized[canonical] = parsed
        elif canonical == "email":
            parsed = _clean_text(value)
            if parsed is None or not _EMAIL_RE.fullmatch(parsed):
                errors[canonical] = "invalid_email"
            else:
                normalized[canonical] = parsed.casefold()
        elif canonical == "phone":
            parsed = _normalize_phone(value)
            if parsed is None:
                errors[canonical] = "invalid_phone"
            else:
                normalized[canonical] = parsed
        elif canonical == "zipcode":
            parsed = normalize_zipcode(value)
            if parsed is None:
                errors[canonical] = "invalid_zipcode"
            else:
                normalized[canonical] = parsed
        elif canonical == "state":
            parsed = _clean_text(value)
            if parsed is None or not re.fullmatch(r"[A-Za-z]{2}", parsed):
                errors[canonical] = "invalid_state"
            else:
                normalized[canonical] = parsed.upper()
        else:
            parsed = _clean_text(value)
            if parsed is None and canonical != "complement":
                errors[canonical] = "empty_value"
            else:
                normalized[canonical] = parsed or ""

    draft = state.checkout_draft.model_copy(deep=True)
    for field, value in normalized.items():
        if field in _CUSTOMER_FIELDS:
            setattr(draft.customer, field, value)
        else:
            setattr(draft.address, "zip_code" if field == "zipcode" else field, value)
    missing = checkout_missing_fields(draft)
    shipping_zipcode_changed = bool(
        normalized.get("zipcode")
        and state.shipping_quote_zipcode
        and normalized["zipcode"] != state.shipping_quote_zipcode
    )
    print("[sales.checkout.data.updated]", {
        "updated_fields": sorted(normalized),
        "cpf_present": bool(draft.customer.cpf),
        "email_present": bool(draft.customer.email),
        "phone_present": bool(draft.customer.phone),
        "address_complete": not any(
            field in missing
            for field in ("address", "zipcode", "number", "neighborhood", "city", "state")
        ),
    })
    print("[sales.checkout.missing_fields]", {
        "missing_count": len(missing),
        "missing_fields": missing,
    })
    return AgentResult(
        reply_text="Dados de checkout atualizados.",
        intent="commerce",
        commercial_data={
            "success": not errors,
            "checkout_fields": checkout_fields_view(draft),
            "required_fields": list(CHECKOUT_REQUIRED_FIELDS),
            "missing_fields": missing,
            "field_errors": errors,
            "shipping_quote_required": shipping_zipcode_changed,
        },
        response_metadata={
            "domain": "commerce",
            "checkout_state": {"checkout_draft": draft.model_dump(mode="json")},
            **(
                {
                    "shipping_state": {
                        "shipping_quote_zipcode": None,
                        "shipping_quotes": [],
                        "selected_shipping": None,
                    },
                    "purchase_stage": "shipping_quote",
                }
                if shipping_zipcode_changed
                else {"purchase_stage": "checkout_data"}
            ),
            **(
                {
                    "pending_action": "awaiting_checkout_data",
                    "pending_action_product_ids": [],
                }
                if missing and not shipping_zipcode_changed
                else {"clear_pending_action": True}
            ),
            "used_tray": False,
        },
    )
