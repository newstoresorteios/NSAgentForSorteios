from __future__ import annotations

from collections.abc import Awaitable, Callable
import re
from typing import Any

import app.catalog.retrieval.runtime as _runtime


PRODUCT_PAGE_LIMIT = 20
CATALOG_DISCOVERY_MAX_PAGES = 5
CATALOG_DISCOVERY_MAX_PRODUCTS = 100
SEMANTIC_MATCH_POOL_LIMIT = 20
CANDIDATE_POOL_LIMIT = SEMANTIC_MATCH_POOL_LIMIT
GPT_MATCH_CANDIDATE_LIMIT = 80
CUSTOMER_RESULT_LIMIT = 3
RERANK_SELECTION_LIMIT = 5  # legacy default; prefer rerank_selection_limit()
MAX_VARIANT_PRODUCT_QUERIES = 5

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def customer_result_limit() -> int:
    """Persona-aware shortlist size (ChatBo recommendation_rules / runtime policy)."""
    limit = CUSTOMER_RESULT_LIMIT
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is not None and getattr(runtime, "max_catalog_options", None):
            limit = int(runtime.max_catalog_options)
    except Exception:
        limit = CUSTOMER_RESULT_LIMIT
    return max(1, min(5, limit))


def _runtime_pattern_matches(attribute: str, message_text: str | None) -> bool:
    if not str(message_text or "").strip():
        return False
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        pattern = str(getattr(runtime, attribute, "") or "").strip()
        return bool(pattern and re.search(pattern, str(message_text), flags=re.IGNORECASE))
    except (re.error, TypeError, ValueError):
        return False


def requests_recommendation_list(message_text: str | None) -> bool:
    """Operator-managed signal that the customer explicitly wants several options."""
    return _runtime_pattern_matches("recommendation_list_request_pattern", message_text)


def requests_next_recommendation(message_text: str | None) -> bool:
    """Operator-managed signal to advance without repeating the current product."""
    return _runtime_pattern_matches("recommendation_next_request_pattern", message_text)


def recommendation_result_limit(
    interpretation: Any | None = None,
    message_text: str | None = None,
) -> int:
    """Choose progressive or shortlist presentation from published workspace policy."""
    maximum = customer_result_limit()
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        if runtime is None or not bool(runtime.progressive_recommendations_enabled):
            return maximum
        goal = str(getattr(interpretation, "goal", "") or "").strip().casefold()
        if goal == "compare" or requests_recommendation_list(message_text):
            return maximum
        progressive = int(runtime.progressive_recommendation_options)
        return max(1, min(maximum, progressive))
    except (TypeError, ValueError, AttributeError):
        return maximum


def prefer_ready_stock_enabled() -> bool:
    try:
        from app.persona.persona_runtime import get_persona_runtime

        runtime = get_persona_runtime()
        return bool(runtime and runtime.prefer_ready_stock)
    except Exception:
        return False


def rerank_selection_limit() -> int:
    settings = _runtime.get_settings()
    from app.persona.persona_runtime import runtime_setting
    try:
        value = int(runtime_setting("catalogRerankLimit", getattr(settings, "agent_rerank_selection_limit", 15)) or 15)
    except (TypeError, ValueError):
        value = 15
    return max(5, min(20, value))


def revalidate_top_n() -> int:
    settings = _runtime.get_settings()
    try:
        value = int(getattr(settings, "agent_revalidate_top_n", CUSTOMER_RESULT_LIMIT) or CUSTOMER_RESULT_LIMIT)
    except (TypeError, ValueError):
        value = CUSTOMER_RESULT_LIMIT
    return max(1, min(10, value))


def candidate_pool_limit() -> int:
    settings = _runtime.get_settings()
    from app.persona.persona_runtime import runtime_setting
    try:
        value = int(runtime_setting("catalogCandidatePool", getattr(settings, "agent_candidate_pool_limit", CANDIDATE_POOL_LIMIT)) or CANDIDATE_POOL_LIMIT)
    except (TypeError, ValueError):
        value = CANDIDATE_POOL_LIMIT
    return max(5, min(80, value))
