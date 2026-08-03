from __future__ import annotations

import json
import re
from typing import Any

from .runtime_context import get_current_turn


_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_PHONE_RE = re.compile(r"\b(?:\+?55)?\s*\(?\d{2}\)?\s*\d{4,5}-?\d{4}\b")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+)\S+|(sk-(?:proj-)?[A-Za-z0-9_-]+)")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def redact_text(value: str | None, *, max_chars: int = 400) -> str:
    text = str(value or "")
    text = _TOKEN_RE.sub(lambda m: (m.group(1) or "") + "***", text)
    text = _CPF_RE.sub("[CPF]", text)
    text = _CNPJ_RE.sub("[CNPJ]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _CARD_RE.sub("[CARD]", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, max_chars=240)
    if isinstance(value, list):
        return [redact_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            key_l = str(key).casefold()
            if any(
                token in key_l
                for token in (
                    "token",
                    "secret",
                    "password",
                    "authorization",
                    "api_key",
                    "cpf",
                    "cnpj",
                    "email",
                    "phone",
                    "tax_document",
                )
            ):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_value(item, depth=depth + 1)
        return redacted
    return redact_text(str(value), max_chars=120)


def summarize_openai_messages(
    messages: list[dict[str, Any]] | None,
    *,
    max_messages: int = 12,
) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for message in (messages or [])[:max_messages]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                else:
                    text_parts.append(str(part))
            content_text = "\n".join(text_parts)
        else:
            content_text = str(content or "")
        summarized.append(
            {
                "role": message.get("role"),
                "chars": len(content_text),
                "preview": redact_text(content_text, max_chars=280),
            }
        )
    return summarized


def summarize_commerce_state(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    payload = (
        state.model_dump(mode="json")
        if hasattr(state, "model_dump")
        else (state if isinstance(state, dict) else {})
    )
    active = payload.get("active_product") if isinstance(payload, dict) else None
    active_name = None
    if isinstance(active, dict):
        active_name = active.get("name")
    return {
        "active_domain": payload.get("active_domain"),
        "purchase_stage": payload.get("purchase_stage"),
        "pending_action": payload.get("pending_action"),
        "has_cart_session": bool(payload.get("cart_session_id")),
        "cart_item_count": len(payload.get("cart_items") or []),
        "presented_product_count": len(payload.get("last_presented_products") or []),
        "active_product_name": redact_text(active_name, max_chars=80) or None,
        "has_order_id": bool(payload.get("order_id")),
        "order_id": payload.get("order_id"),
        "has_payment_url": bool(payload.get("order_payment_url")),
        "order_payment_status": payload.get("order_payment_status"),
        "has_checkout_draft": bool(payload.get("checkout_draft")),
        "checkout_customer_fields": _checkout_customer_fields(payload),
    }


def _checkout_customer_fields(payload: dict[str, Any]) -> list[str]:
    draft = payload.get("checkout_draft") if isinstance(payload, dict) else None
    customer = draft.get("customer") if isinstance(draft, dict) else None
    if not isinstance(customer, dict):
        return []
    return sorted(
        key
        for key, value in customer.items()
        if value not in (None, "", [], {})
    )


def summarize_tray_result(result: dict[str, Any] | None) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    summary: dict[str, Any] = {
        "ok": "error" not in payload,
        "status_code": payload.get("status_code"),
        "error": payload.get("error"),
        "keys": sorted(str(key) for key in payload.keys())[:20],
    }
    for key in (
        "order_id",
        "id",
        "status",
        "status_group",
        "success",
        "match_status",
        "product_count",
        "count",
    ):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            summary[key] = redact_value(payload.get(key))
    payment = payload.get("payment")
    if isinstance(payment, dict):
        summary["payment"] = {
            "status": payment.get("status"),
            "has_payment": payment.get("has_payment"),
            "payment_url_present": bool(payment.get("payment_url")),
        }
    products = payload.get("products")
    if isinstance(products, list):
        summary["products_count"] = len(products)
    orders = payload.get("orders")
    if isinstance(orders, list):
        summary["orders_count"] = len(orders)
    return summary


def log_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Structured turn-aware log line for Vercel/runtime debugging."""
    runtime = get_current_turn()
    body: dict[str, Any] = {
        "event": event,
        "trace_id": runtime.trace_id if runtime else None,
        "inbound_id": runtime.inbound_id if runtime else None,
        "channel": runtime.channel if runtime else None,
        "conversation_key_present": bool(
            runtime
            and runtime.conversation_key
            and runtime.conversation_key != "unresolved"
        ),
    }
    if payload:
        body.update(redact_value(payload) if not isinstance(payload, dict) else {
            key: redact_value(value) for key, value in payload.items()
        })
    # Keep JSON one-line for Vercel log search.
    print(f"[agent.obs] {json.dumps(body, ensure_ascii=False, default=str)}")


def record_tray_observation(
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
    elapsed_ms: float,
) -> None:
    runtime = get_current_turn()
    observation = {
        "tool": tool,
        "ok": isinstance(result, dict) and "error" not in result,
        "elapsed_ms": round(elapsed_ms, 2),
        "arguments": redact_value(arguments or {}),
        "result": summarize_tray_result(result if isinstance(result, dict) else {}),
    }
    if runtime is not None:
        runtime.tray_calls.append(observation)
    log_event("tray.call", observation)


def record_openai_observation(
    *,
    call_type: str,
    model: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    response_preview: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    elapsed_ms: float | None = None,
    ok: bool = True,
    error_type: str | None = None,
) -> None:
    runtime = get_current_turn()
    observation = {
        "call_type": call_type,
        "model": model,
        "ok": ok,
        "error_type": error_type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "elapsed_ms": elapsed_ms,
        "messages": summarize_openai_messages(messages),
        "message_count": len(messages or []),
        "response_preview": redact_text(response_preview, max_chars=280) or None,
    }
    if runtime is not None:
        runtime.openai_calls.append(
            {
                "call_type": call_type,
                "model": model,
                "ok": ok,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "message_count": len(messages or []),
            }
        )
    log_event("openai.call", observation)
