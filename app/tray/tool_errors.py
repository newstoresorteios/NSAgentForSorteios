from __future__ import annotations

from typing import Any


def is_upstream_not_found(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = payload.get("status_code")
    try:
        return int(status) == 404
    except (TypeError, ValueError):
        return False
