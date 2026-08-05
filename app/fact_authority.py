"""Layered factual authorities (Etapa 5).

PolicyAuthority — absolute (security / deterministic rules).
CommerceDataAuthority — Tray live > TTL local/index > snapshot/cache >
  conversation reference. Never persona or memory for price/stock/URL.
ConversationStateAuthority — stage, references, pending actions (not price).
PersonaAuthority — tone/identity only; never commercial numbers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .fact_sources import (
    FACT_SOURCE_RANK,
    FactSource,
    StructuredFact,
)


class AuthorityLayer(str, Enum):
    POLICY = "policy"
    COMMERCE = "commerce"
    CONVERSATION = "conversation"
    PERSONA = "persona"


class RevalidationStatus(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    REVALIDATED = "revalidated"
    STALE = "stale"
    FAILED = "failed"
    SKIPPED = "skipped"


COMMERCIAL_ENTITY_TYPES = frozenset(
    {"product", "variant", "price", "inventory", "url", "payment", "shipping", "cart"}
)

# Persona / memory must never win commercial conflicts.
COMMERCE_FORBIDDEN_SOURCES = frozenset(
    {FactSource.APPROVED_PERSONA, FactSource.CUSTOMER_MEMORY}
)


class CommercialClaim(BaseModel):
    """Every commercial assertion carried toward the customer."""

    kind: Literal[
        "price",
        "promotional_price",
        "stock",
        "availability",
        "url",
        "product",
        "variant",
        "payment",
        "shipping",
        "other",
    ]
    value: Any = None
    source: FactSource
    freshness_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tenant_id: str = "newstore"
    product_id: str | None = None
    variant_id: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    revalidation_status: RevalidationStatus = RevalidationStatus.PENDING
    authority_layer: AuthorityLayer = AuthorityLayer.COMMERCE
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_commerce_safe(self) -> bool:
        if self.kind in {
            "price",
            "promotional_price",
            "stock",
            "availability",
            "url",
            "product",
            "variant",
        }:
            return self.source not in COMMERCE_FORBIDDEN_SOURCES
        return True


class PolicyAuthority:
    """Absolute rules — never overridden by persona, memory, or Tray."""

    layer = AuthorityLayer.POLICY

    @staticmethod
    def source_rank(source: FactSource) -> int:
        if source in {FactSource.SECURITY_RULE, FactSource.DETERMINISTIC_RULE}:
            return FACT_SOURCE_RANK[source]
        return 0

    @staticmethod
    def allows_persona_to_state_commercial_facts() -> bool:
        return False


class CommerceDataAuthority:
    """Commercial numbers and product identity.

    Priority:
    1. Tray live revalidation
    2. Tray adapter / search (same turn)
    3. Local DB / catalog index within TTL
    4. Snapshot / cache
    5. Conversation reference (identity only — not price/stock as absolute)
    Never: persona, customer memory
    """

    layer = AuthorityLayer.COMMERCE

    SOURCE_PRIORITY: tuple[FactSource, ...] = (
        FactSource.TRAY_LIVE,
        FactSource.TRAY_ADAPTER,
        FactSource.LOCAL_DATABASE,
        FactSource.CATALOG_SNAPSHOT,
        FactSource.COMMERCE_STATE,
    )

    @classmethod
    def rank(cls, source: FactSource) -> int:
        try:
            return 100 - cls.SOURCE_PRIORITY.index(source)
        except ValueError:
            return FACT_SOURCE_RANK.get(source, 0)

    @classmethod
    def may_supply(cls, entity_type: str, source: FactSource) -> bool:
        if entity_type not in COMMERCIAL_ENTITY_TYPES:
            return True
        return source not in COMMERCE_FORBIDDEN_SOURCES

    @classmethod
    def prefer(
        cls,
        facts: list[StructuredFact],
        *,
        entity_type: str | None = None,
        key: str | None = None,
    ) -> StructuredFact | None:
        candidates = [
            fact
            for fact in facts
            if (entity_type is None or fact.entity_type == entity_type)
            and (key is None or fact.key == key)
            and cls.may_supply(fact.entity_type, fact.source)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                cls.rank(item.source),
                1 if (item.metadata or {}).get("revalidated") else 0,
                item.confidence or 0.0,
            ),
        )


class ConversationStateAuthority:
    """Conversation stage, presented products, pending actions — not price truth."""

    layer = AuthorityLayer.CONVERSATION

    ALLOWED_AS_ABSOLUTE = frozenset(
        {
            "pending_action",
            "purchase_stage",
            "active_product_id",
            "active_product_name",
            "presented_product_ids",
            "order_id",
            "order_lookup_id",
        }
    )

    @classmethod
    def may_assert_price_or_stock(cls) -> bool:
        return False


class PersonaAuthority:
    """Tone and identity only."""

    layer = AuthorityLayer.PERSONA

    @classmethod
    def may_assert_commercial_fact(cls) -> bool:
        return False


def claim_from_product_field(
    product: dict[str, Any],
    *,
    kind: Literal["price", "promotional_price", "stock", "availability", "url", "product"],
    key: str,
    value: Any,
    tenant_id: str = "newstore",
) -> CommercialClaim:
    factual = str(product.get("_factual_source") or "").strip().lower()
    revalidated = bool(product.get("_revalidated"))
    if factual == "tray_live" or revalidated:
        source = FactSource.TRAY_LIVE
        status = RevalidationStatus.REVALIDATED
        confidence = 0.95
    elif factual in {"catalog_cache", "catalog_index"}:
        source = FactSource.CATALOG_SNAPSHOT
        status = RevalidationStatus.STALE
        confidence = 0.6
    elif factual == "conversation_ref":
        source = FactSource.COMMERCE_STATE
        status = RevalidationStatus.NOT_APPLICABLE
        confidence = 0.4
    else:
        source = FactSource.TRAY_ADAPTER
        status = (
            RevalidationStatus.REVALIDATED
            if revalidated
            else RevalidationStatus.PENDING
        )
        confidence = 0.75 if revalidated else 0.55

    freshness = product.get("_freshness_at") or product.get("freshness_at")
    if isinstance(freshness, str):
        try:
            freshness_at = datetime.fromisoformat(freshness.replace("Z", "+00:00"))
        except ValueError:
            freshness_at = datetime.now(timezone.utc)
    elif isinstance(freshness, datetime):
        freshness_at = freshness
    else:
        freshness_at = datetime.now(timezone.utc)

    return CommercialClaim(
        kind=kind,
        value=value,
        source=source,
        freshness_at=freshness_at,
        tenant_id=tenant_id,
        product_id=str(product.get("id")) if product.get("id") is not None else None,
        variant_id=(
            str(product.get("variant_id"))
            if product.get("variant_id") is not None
            else None
        ),
        confidence=confidence,
        revalidation_status=status,
        authority_layer=AuthorityLayer.COMMERCE,
        metadata={"field": key, "factual_source_raw": factual or None},
    )


def filter_commerce_safe_evidence(
    facts: list[StructuredFact],
) -> list[StructuredFact]:
    """Drop persona/memory evidence for commercial entity types."""
    safe: list[StructuredFact] = []
    for fact in facts:
        if (
            fact.entity_type in COMMERCIAL_ENTITY_TYPES
            and fact.source in COMMERCE_FORBIDDEN_SOURCES
        ):
            continue
        safe.append(fact)
    return safe
