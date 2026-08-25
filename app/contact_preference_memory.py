"""Deterministic contact preference / theme memory (not LLM reply copy).

Persists structured facts from SalesInterpretation into ai_contact_memories so
the next contact can reuse brand, style, budget, occasion, and last theme —
without dumping full chat history or inventing persona text.

Also rehydrates empty interpretation fields from active memories so Tray search
and qualification use prior context (current message always wins).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import get_settings
from .contact_memory_repository import (
    get_active_contact_memories,
    upsert_contact_memory,
)
from .memory_models import ContactMemory, ConversationSummaryDelta, MemoryKind
from .models import SalesInterpretation

_MOVEMENT_LABELS = frozenset(
    {
        "automatico",
        "automático",
        "automatic",
        "quartz",
        "quartzo",
        "manual",
        "mecanico",
        "mecânico",
        "kinetic",
        "solar",
    }
)
_GENERIC_THEME = "interesse comercial em relógios"
_MAX_BRANDS = 5


def _fold_label(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _fold_key(value: Any) -> str:
    return _fold_label(value).casefold()


def _unwrap_value(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw and len(raw) == 1:
        return raw["value"]
    return raw


def _preference_ttl_days() -> int:
    settings = get_settings()
    try:
        days = int(getattr(settings, "agent_contact_preference_ttl_days", 60) or 60)
    except (TypeError, ValueError):
        days = 60
    return max(1, min(days, 365))


def _theme_ttl_days() -> int:
    settings = get_settings()
    try:
        days = int(getattr(settings, "agent_contact_theme_ttl_days", 30) or 30)
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 180))


def _expires_at(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _min_write_confidence() -> float:
    settings = get_settings()
    try:
        value = float(
            getattr(settings, "agent_contact_preference_min_confidence", 0.7) or 0.7
        )
    except (TypeError, ValueError):
        value = 0.7
    return max(0.0, min(value, 1.0))


def should_persist_interpretation(interpretation: SalesInterpretation) -> bool:
    """Quality gate: skip weak / empty / greeting-only turns."""
    if interpretation.domain not in {"commerce", "store_general"}:
        return False
    goal = (interpretation.goal or "").strip()
    if goal in {"", "after_sales"} and not interpretation.subject.brand:
        return False
    confidence = float(interpretation.confidence or 0.0)
    has_signal = any(
        (
            interpretation.subject.brand,
            interpretation.subject.model,
            interpretation.preferences.style,
            interpretation.preferences.color,
            interpretation.preferences.occasion,
            interpretation.preferences.budget_min is not None,
            interpretation.preferences.budget_max is not None,
        )
    )
    if not has_signal:
        return False
    if confidence >= _min_write_confidence():
        return True
    return bool(
        interpretation.enough_information_to_search
        or interpretation.ready_for_retrieval
        or interpretation.subject.brand
    )


def _merge_brand_list(
    existing: ContactMemory | None,
    brand: str,
) -> dict[str, Any]:
    brands: list[str] = []
    if existing is not None:
        raw = _unwrap_value(existing.value)
        if isinstance(raw, dict):
            prior = raw.get("brands") or raw.get("value")
            if isinstance(prior, list):
                brands = [_fold_label(item) for item in prior if _fold_label(item)]
            elif isinstance(prior, str) and prior.strip():
                brands = [_fold_label(prior)]
        elif isinstance(raw, str) and raw.strip():
            brands = [_fold_label(raw)]
        elif isinstance(raw, list):
            brands = [_fold_label(item) for item in raw if _fold_label(item)]
    brand = _fold_label(brand)
    brands = [item for item in brands if _fold_key(item) != _fold_key(brand)]
    brands.insert(0, brand)
    brands = brands[:_MAX_BRANDS]
    return {"brands": brands, "active": brand}


def build_preference_memory_items(
    interpretation: SalesInterpretation,
    *,
    existing_memories: list[ContactMemory] | None = None,
) -> list[dict[str, Any]]:
    """Map interpreter fields → durable contact memory rows."""
    prefs = interpretation.preferences
    subject = interpretation.subject
    existing_by_key = {
        item.memory_key: item for item in (existing_memories or []) if item.memory_key
    }
    items: list[dict[str, Any]] = []
    preference_ttl = _expires_at(_preference_ttl_days())
    theme_ttl = _expires_at(_theme_ttl_days())

    def add(
        *,
        memory_key: str,
        memory_kind: str,
        value: Any,
        summary: str,
        importance: float = 0.8,
        expires_at: datetime | None = None,
    ) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        if isinstance(value, (list, tuple)) and not value:
            return
        items.append(
            {
                "memory_key": memory_key[:120],
                "memory_kind": memory_kind,
                "value": value if isinstance(value, (dict, list)) else {"value": value},
                "safe_summary": summary[:240],
                "importance": importance,
                "confidence": max(float(interpretation.confidence or 0.0), 0.75),
                "use_in_instructions": True,
                "expires_at": expires_at or preference_ttl,
            }
        )

    brand = _fold_label(subject.brand)
    if brand:
        merged = _merge_brand_list(existing_by_key.get("brand_preference"), brand)
        brand_label = ", ".join(merged["brands"][:3])
        add(
            memory_key="brand_preference",
            memory_kind=MemoryKind.brand_preference.value,
            value=merged,
            summary=f"brand={brand_label}",
            importance=0.9,
        )

    style = _fold_label(prefs.style)
    if style:
        add(
            memory_key="style_preference",
            memory_kind=MemoryKind.product_preference.value,
            value=style,
            summary=f"style={style}",
        )

    color = _fold_label(prefs.color)
    if color:
        add(
            memory_key="color_preference",
            memory_kind=MemoryKind.color_preference.value,
            value=color,
            summary=f"color={color}",
        )

    material = _fold_label(prefs.material)
    if material and _fold_key(material) not in _MOVEMENT_LABELS:
        add(
            memory_key="material_preference",
            memory_kind=MemoryKind.material_preference.value,
            value=material,
            summary=f"material={material}",
        )
    elif material and _fold_key(material) in _MOVEMENT_LABELS:
        add(
            memory_key="movement_preference",
            memory_kind=MemoryKind.product_preference.value,
            value=material,
            summary=f"movement={material}",
            importance=0.72,
        )

    occasion = _fold_label(prefs.occasion)
    if occasion:
        add(
            memory_key="occasion",
            memory_kind=MemoryKind.occasion.value,
            value=occasion,
            summary=f"occasion={occasion}",
        )

    recipient = _fold_label(prefs.recipient)
    if recipient:
        add(
            memory_key="recipient",
            memory_kind=MemoryKind.recipient.value,
            value=recipient,
            summary=f"recipient={recipient}",
        )

    if prefs.budget_min is not None or prefs.budget_max is not None:
        budget = {
            "min": prefs.budget_min,
            "max": prefs.budget_max,
        }
        if prefs.budget_max is not None and prefs.budget_min is not None:
            label = f"budget={prefs.budget_min:g}-{prefs.budget_max:g}"
        elif prefs.budget_max is not None:
            label = f"budget_max={prefs.budget_max:g}"
        else:
            label = f"budget_min={prefs.budget_min:g}"
        add(
            memory_key="price_preference",
            memory_kind=MemoryKind.price_preference.value,
            value=budget,
            summary=label,
            importance=0.88,
        )

    for raw in list(prefs.explicit_no_preferences or []):
        key = _fold_key(raw).replace(" ", "_")[:80]
        if not key:
            continue
        add(
            memory_key=f"explicit_no:{key}",
            memory_kind=MemoryKind.explicit_no_preference.value,
            value=_fold_label(raw),
            summary=f"explicit_no={_fold_label(raw)}",
            importance=0.75,
        )

    theme_parts: list[str] = []
    if brand:
        theme_parts.append(brand)
    model = _fold_label(subject.model)
    if model:
        theme_parts.append(model)
    if style:
        theme_parts.append(style)
    if color:
        theme_parts.append(color)
    if material and _fold_key(material) not in _MOVEMENT_LABELS:
        theme_parts.append(material)
    elif material:
        theme_parts.append(material)
    if prefs.budget_max is not None:
        theme_parts.append(f"budget_max~{prefs.budget_max:g}")
    elif prefs.budget_min is not None:
        theme_parts.append(f"budget_min~{prefs.budget_min:g}")
    if occasion:
        theme_parts.append(f"occasion={occasion}")

    # Never persist a generic commerce theme without real signals.
    if theme_parts:
        theme = ", ".join(theme_parts)
        if theme.casefold() != _GENERIC_THEME:
            add(
                memory_key="last_commerce_theme",
                memory_kind=MemoryKind.conversation_goal.value,
                value={
                    "theme": theme,
                    "goal": interpretation.goal or None,
                    "product_type": _fold_label(subject.product_type) or None,
                },
                summary=f"theme={theme}",
                importance=0.95,
                expires_at=theme_ttl,
            )

    return items


def build_summary_delta_from_interpretation(
    interpretation: SalesInterpretation,
    *,
    existing_memories: list[ContactMemory] | None = None,
) -> ConversationSummaryDelta | None:
    """Compact continuity summary — preferences only, no live price/stock."""
    items = build_preference_memory_items(
        interpretation,
        existing_memories=existing_memories,
    )
    if not items:
        return None
    theme = next(
        (
            str((item.get("value") or {}).get("theme") or item.get("safe_summary") or "")
            for item in items
            if item.get("memory_key") == "last_commerce_theme"
        ),
        None,
    )
    return ConversationSummaryDelta(
        current_goal=theme,
        open_questions=[],
        resolved_points=[
            str(item.get("safe_summary") or "")
            for item in items
            if item.get("memory_key") != "last_commerce_theme"
            and item.get("safe_summary")
        ][:8],
    )


def _active_brand_from_memory(memory: ContactMemory) -> str | None:
    raw = _unwrap_value(memory.value)
    if isinstance(raw, dict):
        active = _fold_label(raw.get("active") or "")
        if active:
            return active
        brands = raw.get("brands")
        if isinstance(brands, list) and brands:
            return _fold_label(brands[0])
        return _fold_label(raw.get("value") or "") or None
    if isinstance(raw, list) and raw:
        return _fold_label(raw[0]) or None
    if isinstance(raw, str):
        return _fold_label(raw) or None
    return None


def rehydrate_interpretation_from_memories(
    interpretation: SalesInterpretation,
    memories: list[ContactMemory],
) -> tuple[SalesInterpretation, list[str]]:
    """Fill empty preference/subject fields from durable contact memory.

    Current-turn interpretation always wins: we never overwrite non-empty fields.
    """
    if not memories:
        return interpretation, []
    prefs = interpretation.preferences.model_copy(deep=True)
    subject = interpretation.subject.model_copy(deep=True)
    filled: list[str] = []

    by_key = {item.memory_key: item for item in memories if item.memory_key}

    brand_mem = by_key.get("brand_preference")
    if brand_mem and not subject.brand:
        brand = _active_brand_from_memory(brand_mem)
        if brand:
            subject.brand = brand
            filled.append("brand")

    style_mem = by_key.get("style_preference")
    if style_mem and not prefs.style:
        value = _fold_label(_unwrap_value(style_mem.value))
        if value:
            prefs.style = value
            filled.append("style")

    color_mem = by_key.get("color_preference")
    if color_mem and not prefs.color:
        value = _fold_label(_unwrap_value(color_mem.value))
        if value:
            prefs.color = value
            filled.append("color")

    material_mem = by_key.get("material_preference")
    if material_mem and not prefs.material:
        value = _fold_label(_unwrap_value(material_mem.value))
        if value:
            prefs.material = value
            filled.append("material")

    movement_mem = by_key.get("movement_preference")
    if movement_mem:
        value = _fold_label(_unwrap_value(movement_mem.value))
        if value and value not in list(prefs.attributes or []):
            prefs.attributes = list(prefs.attributes or []) + [value]
            filled.append("movement")

    occasion_mem = by_key.get("occasion")
    if occasion_mem and not prefs.occasion:
        value = _fold_label(_unwrap_value(occasion_mem.value))
        if value:
            prefs.occasion = value
            filled.append("occasion")

    recipient_mem = by_key.get("recipient")
    if recipient_mem and not prefs.recipient:
        value = _fold_label(_unwrap_value(recipient_mem.value))
        if value:
            prefs.recipient = value
            filled.append("recipient")

    price_mem = by_key.get("price_preference")
    if price_mem and prefs.budget_min is None and prefs.budget_max is None:
        raw = _unwrap_value(price_mem.value)
        if isinstance(raw, dict):
            try:
                if raw.get("min") is not None:
                    prefs.budget_min = float(raw["min"])
                    filled.append("budget_min")
                if raw.get("max") is not None:
                    prefs.budget_max = float(raw["max"])
                    filled.append("budget_max")
            except (TypeError, ValueError):
                pass

    for memory in memories:
        if not str(memory.memory_key or "").startswith("explicit_no:"):
            continue
        value = _fold_label(_unwrap_value(memory.value))
        if not value:
            continue
        current = list(prefs.explicit_no_preferences or [])
        if value not in current:
            prefs.explicit_no_preferences = current + [value]
            filled.append(f"explicit_no:{value}")

    if not filled:
        return interpretation, []

    # Returning contact with prior commerce context: avoid re-asking dims we already know.
    updates: dict[str, Any] = {
        "preferences": prefs,
        "subject": subject,
    }
    if interpretation.domain == "commerce" and (
        subject.brand or prefs.style or prefs.budget_max is not None
    ):
        # Soft unlock signal for discovery — persona gate still applies if brand-only.
        if interpretation.goal in {None, "discover", ""} and subject.brand:
            updates["goal"] = "find"
    return interpretation.model_copy(update=updates), filled


def rehydrate_interpretation_from_contact_memory(
    interpretation: SalesInterpretation,
    *,
    tenant_id: str,
    sender_key: str | None,
) -> SalesInterpretation:
    """Load active memories and fill empty interpretation fields."""
    settings = get_settings()
    if not bool(getattr(settings, "agent_contact_preference_memory_enabled", True)):
        return interpretation
    if not bool(getattr(settings, "agent_contact_preference_rehydrate_enabled", True)):
        return interpretation
    if not sender_key:
        return interpretation
    try:
        memories = get_active_contact_memories(
            tenant_id=tenant_id,
            sender_key=sender_key,
            limit=int(getattr(settings, "agent_max_active_contact_memories", 20)),
        )
    except Exception as exc:
        print(
            "[memory.contact_preference.rehydrate_load_error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:160]},
        )
        return interpretation
    updated, filled = rehydrate_interpretation_from_memories(interpretation, memories)
    if filled:
        print(
            "[memory.contact_preference.rehydrated]",
            {"filled": filled[:12], "sender_key_present": True},
        )
    return updated


def persist_contact_preferences_from_interpretation(
    *,
    tenant_id: str,
    sender_key: str | None,
    conversation_key: str | None,
    interpretation: SalesInterpretation | None,
    inbound_id: int | None = None,
    response_id: int | None = None,
) -> dict[str, Any]:
    """Upsert durable preferences + optional conversation summary continuity."""
    settings = get_settings()
    if not bool(getattr(settings, "agent_contact_preference_memory_enabled", True)):
        return {"enabled": False, "upserted": 0}
    if not sender_key or interpretation is None:
        return {"enabled": True, "upserted": 0, "skipped": "missing_sender_or_interpretation"}
    if not should_persist_interpretation(interpretation):
        return {"enabled": True, "upserted": 0, "skipped": "quality_gate"}

    existing: list[ContactMemory] = []
    try:
        existing = get_active_contact_memories(
            tenant_id=tenant_id,
            sender_key=sender_key,
            limit=int(getattr(settings, "agent_max_active_contact_memories", 20)),
        )
    except Exception as exc:
        print(
            "[memory.contact_preference.load_existing_error]",
            {"error_type": type(exc).__name__, "error": str(exc)[:120]},
        )

    items = build_preference_memory_items(
        interpretation,
        existing_memories=existing,
    )
    upserted = 0
    keys: list[str] = []
    for item in items:
        try:
            upsert_contact_memory(
                tenant_id=tenant_id,
                sender_key=sender_key,
                memory_key=str(item["memory_key"]),
                memory_kind=str(item["memory_kind"]),
                value=item["value"],
                safe_summary=str(item.get("safe_summary") or ""),
                source="deterministic_interpretation",
                importance=float(item.get("importance") or 0.8),
                confidence=float(item.get("confidence") or 0.9),
                use_in_instructions=True,
                source_inbound_id=inbound_id,
                source_response_id=response_id,
                expires_at=item.get("expires_at"),
                metadata={"origin": "contact_preference_memory"},
            )
            upserted += 1
            keys.append(str(item["memory_key"]))
        except Exception as exc:
            print(
                "[memory.contact_preference.upsert_error]",
                {
                    "error_type": type(exc).__name__,
                    "memory_key": item.get("memory_key"),
                    "error": str(exc)[:160],
                },
            )

    if upserted:
        try:
            from .memory_consolidation import consolidate_contact_memories

            consolidate_contact_memories(tenant_id=tenant_id, sender_key=sender_key)
        except Exception as exc:
            print(
                "[memory.contact_preference.consolidation_error]",
                {"error_type": type(exc).__name__, "error": str(exc)[:120]},
            )

    summary_written = False
    if conversation_key and bool(
        getattr(settings, "agent_contact_preference_summary_enabled", True)
    ):
        delta = build_summary_delta_from_interpretation(
            interpretation,
            existing_memories=existing,
        )
        if delta is not None:
            try:
                from .conversation_summary_repository import apply_summary_delta

                apply_summary_delta(
                    tenant_id=tenant_id,
                    conversation_key=conversation_key,
                    delta=delta,
                    inbound_id=inbound_id,
                    response_id=response_id,
                    max_chars=int(
                        getattr(settings, "agent_max_conversation_summary_chars", 2500)
                    ),
                )
                summary_written = True
            except Exception as exc:
                print(
                    "[memory.contact_preference.summary_error]",
                    {"error_type": type(exc).__name__, "error": str(exc)[:160]},
                )

    print(
        "[memory.contact_preference.persisted]",
        {
            "upserted": upserted,
            "keys": keys[:12],
            "summary_written": summary_written,
            "sender_key_present": True,
        },
    )
    return {
        "enabled": True,
        "upserted": upserted,
        "keys": keys,
        "summary_written": summary_written,
    }
