"""Pré-tool policy for Tray mutations.

Post-response ``evaluate_policy`` stays in the pipeline. This gate runs
inside ``execute_tool`` before create_cart / create_order / quantity /
delete_cart. Default is shadow: log a would-block and still call Tray.
Do not flip enforce in production without goldens.
"""

from __future__ import annotations

from typing import Any, Literal

from app.config import get_settings

MUTATING_TOOLS = frozenset(
    {
        "create_cart",
        "create_order",
        "set_cart_item_quantity",
        "delete_cart",
    }
)

ToolPolicyAction = Literal["allow", "block"]


def _has_product_id(arguments: dict[str, Any]) -> bool:
    token = arguments.get("product_id") or arguments.get("id")
    if token is not None and str(token).strip():
        return True
    for key in ("items", "products"):
        rows = arguments.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            nested = row.get("product_id") or row.get("id")
            if nested is not None and str(nested).strip():
                return True
    return False


def _has_cart_handle(arguments: dict[str, Any]) -> bool:
    for key in ("session_id", "cart_session_id", "cart_id"):
        token = arguments.get(key)
        if token is not None and str(token).strip():
            return True
    payload = arguments.get("payload")
    if isinstance(payload, dict):
        return _has_cart_handle(payload)
    return False


def evaluate_tool_policy(
    name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    args = arguments if isinstance(arguments, dict) else {}
    reasons: list[str] = []
    if name == "create_cart" and not _has_product_id(args):
        reasons.append("create_cart_missing_product")
    elif name == "create_order" and not args:
        reasons.append("create_order_empty_payload")
    elif name == "set_cart_item_quantity" and not (
        _has_cart_handle(args) and _has_product_id(args)
    ):
        reasons.append("cart_quantity_missing_target")
    elif name == "delete_cart" and not _has_cart_handle(args):
        reasons.append("delete_cart_missing_session")
    action: ToolPolicyAction = "block" if reasons else "allow"
    return {
        "tool": name,
        "action": action,
        "reasons": reasons,
    }


def apply_tool_policy(
    name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an error payload only when mode is enforce and the tool is blocked.

    Shadow logs and returns None so Tray still runs.
    """
    if name not in MUTATING_TOOLS:
        return None
    try:
        settings = get_settings()
        mode = str(getattr(settings, "agent_tool_policy_mode", "shadow") or "shadow")
    except Exception:
        mode = "shadow"
    mode = mode.strip().casefold()
    if mode not in {"off", "shadow", "enforce"}:
        mode = "shadow"
    if mode == "off":
        return None
    verdict = evaluate_tool_policy(name, arguments)
    if verdict["action"] != "block":
        return None
    print(
        "[sales.tool.policy]",
        {
            "tool": name,
            "mode": mode,
            "would_block": True,
            "enforced": mode == "enforce",
            "reasons": verdict["reasons"],
        },
    )
    if mode != "enforce":
        return None
    return {
        "error": "tool_policy_blocked",
        "error_type": "tool_policy_blocked",
        "policy_reasons": list(verdict["reasons"]),
        "tool": name,
    }
