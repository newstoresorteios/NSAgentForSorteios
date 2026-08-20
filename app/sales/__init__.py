"""Incremental sales package extracted from ``sales_agent`` (Phase 11 / IQ-08).

Public entry points remain on ``app.sales_agent`` for compatibility.
"""

from .checkout_flow import respond_to_commerce_service
from .discovery import build_qualification_snapshot
from .policies.confirmation import confirmation_text_kind
from .product_lookup import (
    execute_compiled_product_retrieval,
    execute_contextual_product_lookup,
)
from .result_utils import mark_sales_result
from .workflows.catalog_ranking import rank_candidates, score_candidate

__all__ = [
    "build_qualification_snapshot",
    "confirmation_text_kind",
    "execute_compiled_product_retrieval",
    "execute_contextual_product_lookup",
    "mark_sales_result",
    "rank_candidates",
    "respond_to_commerce_service",
    "score_candidate",
]
