"""Deterministic contact preference / theme memory (not LLM reply copy).

Persists structured facts from SalesInterpretation into ai_contact_memories so
the next contact can reuse brand, style, budget, occasion, and last theme —
without dumping full chat history or inventing persona text.
"""

from __future__ import annotations

from typing import Any

from .config import get_settings
from .contact_memory_repository import upsert_contact_memory
from .memory_models import ConversationSummaryDelta, MemoryKind
from .models import SalesInterpretation


def _fold_label(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def build_preference_memory_items(
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Map interpreter fields → durable contact memory rows."""
    prefs = interpretation.preferences
    subject = interpretation.subject
    items: list[dict[str, Any]] = []

    def add(
        *,
        memory_key: str,
        memory_kind: str,
        value: Any,
        summary: str,
        importance: float = 0.8,
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
                "confidence": 0.92,
                "use_in_instructions": True,
            }
        )

    brand = _fold_label(subject.brand)
    if brand:
        add(
            memory_key="brand_preference",
            memory_kind=MemoryKind.brand_preference.value,
            value=brand,
            summary=f"Marca de interesse: {brand}",
            importance=0.9,
        )

    style = _fold_label(prefs.style)
    if style:
        add(
            memory_key="style_preference",
            memory_kind=MemoryKind.product_preference.value,
            value=style,
            summary=f"Estilo preferido: {style}",
        )

    color = _fold_label(prefs.color)
    if color:
        add(
            memory_key="color_preference",
            memory_kind=MemoryKind.color_preference.value,
            value=color,
            summary=f"Cor/acabamento: {color}",
        )

    material = _fold_label(prefs.material)
    if material:
        add(
            memory_key="material_preference",
            memory_kind=MemoryKind.material_preference.value,
            value=material,
            summary=f"Material: {material}",
        )

    occasion = _fold_label(prefs.occasion)
    if occasion:
        add(
            memory_key="occasion",
            memory_kind=MemoryKind.occasion.value,
            value=occasion,
            summary=f"Ocasião de uso: {occasion}",
        )

    recipient = _fold_label(prefs.recipient)
    if recipient:
        add(
            memory_key="recipient",
            memory_kind=MemoryKind.recipient.value,
            value=recipient,
            summary=f"Para quem: {recipient}",
        )

    if prefs.budget_min is not None or prefs.budget_max is not None:
        budget = {
            "min": prefs.budget_min,
            "max": prefs.budget_max,
        }
        if prefs.budget_max is not None and prefs.budget_min is not None:
            label = f"Faixa R$ {prefs.budget_min:g}–{prefs.budget_max:g}"
        elif prefs.budget_max is not None:
            label = f"Até cerca de R$ {prefs.budget_max:g}"
        else:
            label = f"A partir de cerca de R$ {prefs.budget_min:g}"
        add(
            memory_key="price_preference",
            memory_kind=MemoryKind.price_preference.value,
            value=budget,
            summary=label,
            importance=0.88,
        )

    for raw in list(prefs.explicit_no_preferences or []):
        key = _fold_label(raw).casefold().replace(" ", "_")[:80]
        if not key:
            continue
        add(
            memory_key=f"explicit_no:{key}",
            memory_kind=MemoryKind.explicit_no_preference.value,
            value=_fold_label(raw),
            summary=f"Sem preferência em: {_fold_label(raw)}",
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
    if material:
        theme_parts.append(material)
    if prefs.budget_max is not None:
        theme_parts.append(f"até ~R${prefs.budget_max:g}")
    elif prefs.budget_min is not None:
        theme_parts.append(f"desde ~R${prefs.budget_min:g}")
    if occasion:
        theme_parts.append(f"ocasião {occasion}")

    goal = interpretation.goal or ""
    if theme_parts or goal in {"discover", "find", "recommend", "compare", "buy"}:
        theme = ", ".join(theme_parts) if theme_parts else "interesse comercial em relógios"
        add(
            memory_key="last_commerce_theme",
            memory_kind=MemoryKind.conversation_goal.value,
            value={
                "theme": theme,
                "goal": goal or None,
                "product_type": _fold_label(subject.product_type) or None,
            },
            summary=f"Último tema de interesse: {theme}",
            importance=0.95,
        )

    return items


def build_summary_delta_from_interpretation(
    interpretation: SalesInterpretation,
) -> ConversationSummaryDelta | None:
    """Compact continuity summary — preferences only, no live price/stock."""
    items = build_preference_memory_items(interpretation)
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
    open_questions: list[str] = []
    prefs = interpretation.preferences
    if not prefs.budget_min and not prefs.budget_max and interpretation.subject.brand:
        open_questions.append("faixa de investimento")
    if not prefs.style and not prefs.occasion and interpretation.subject.brand:
        open_questions.append("estilo ou ocasião de uso")
    return ConversationSummaryDelta(
        current_goal=theme,
        open_questions=open_questions[:4],
        resolved_points=[
            str(item.get("safe_summary") or "")
            for item in items
            if item.get("memory_key") != "last_commerce_theme"
            and item.get("safe_summary")
        ][:8],
    )


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

    items = build_preference_memory_items(interpretation)
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
        delta = build_summary_delta_from_interpretation(interpretation)
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
