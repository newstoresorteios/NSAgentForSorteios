from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.memory.contact_preference_memory import (
    build_preference_memory_items,
    persist_contact_preferences_from_interpretation,
    rehydrate_interpretation_from_memories,
    should_persist_interpretation,
)
from app.memory.memory_models import ContactMemory
from app.models import SalesInterpretation
from tests.memory_fakes import InMemoryMemoryStore


def _interp(**kwargs) -> SalesInterpretation:
    base = {
        "domain": "commerce",
        "goal": "find",
        "subject": {},
        "preferences": {},
        "references_previous_context": False,
        "enough_information_to_search": False,
        "ready_for_retrieval": False,
        "needs_clarification": False,
        "confidence": 0.9,
    }
    base.update(kwargs)
    return SalesInterpretation(**base)


def test_build_preference_memory_items_from_bulova_turn():
    interpretation = _interp(
        subject={"product_type": "relógio", "brand": "Bulova"},
        preferences={
            "color": "dourado",
            "material": "automático",
            "budget_max": 5000,
            "style": "clássico",
        },
        enough_information_to_search=True,
        ready_for_retrieval=True,
    )
    items = build_preference_memory_items(interpretation)
    keys = {item["memory_key"] for item in items}
    assert "brand_preference" in keys
    assert "color_preference" in keys
    assert "style_preference" in keys
    assert "price_preference" in keys
    assert "movement_preference" in keys
    assert "material_preference" not in keys
    assert "last_commerce_theme" in keys
    theme = next(item for item in items if item["memory_key"] == "last_commerce_theme")
    assert "Bulova" in theme["safe_summary"]
    assert theme["safe_summary"].startswith("theme=")
    assert item_has_expiry(items)


def item_has_expiry(items: list[dict]) -> bool:
    return all(item.get("expires_at") is not None for item in items)


def test_quality_gate_skips_generic_and_weak_turns():
    weak = _interp(goal="discover", confidence=0.2, subject={}, preferences={})
    assert should_persist_interpretation(weak) is False

    brand_only = _interp(
        subject={"brand": "Bulova"},
        confidence=0.4,
        enough_information_to_search=False,
    )
    assert should_persist_interpretation(brand_only) is True

    items = build_preference_memory_items(
        _interp(goal="find", confidence=0.95, subject={}, preferences={})
    )
    assert "last_commerce_theme" not in {item["memory_key"] for item in items}


def test_multi_brand_merge_keeps_recent_active():
    existing = [
        ContactMemory(
            id=1,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="brand_preference",
            memory_kind="brand_preference",
            value={"brands": ["Seiko", "Tissot"], "active": "Seiko"},
            safe_summary="brand=Seiko, Tissot",
            use_in_instructions=True,
        )
    ]
    items = build_preference_memory_items(
        _interp(subject={"brand": "Bulova"}),
        existing_memories=existing,
    )
    brand = next(item for item in items if item["memory_key"] == "brand_preference")
    assert brand["value"]["active"] == "Bulova"
    assert brand["value"]["brands"][:3] == ["Bulova", "Seiko", "Tissot"]


def test_rehydrate_fills_empty_fields_without_overwriting():
    memories = [
        ContactMemory(
            id=1,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="brand_preference",
            memory_kind="brand_preference",
            value={"brands": ["Bulova", "Seiko"], "active": "Bulova"},
            safe_summary="brand=Bulova, Seiko",
            use_in_instructions=True,
        ),
        ContactMemory(
            id=2,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="price_preference",
            memory_kind="price_preference",
            value={"min": None, "max": 4500},
            safe_summary="budget_max=4500",
            use_in_instructions=True,
        ),
        ContactMemory(
            id=3,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="style_preference",
            memory_kind="product_preference",
            value={"value": "clássico"},
            safe_summary="style=clássico",
            use_in_instructions=True,
        ),
    ]
    interpretation = _interp(
        subject={"brand": "Hamilton"},
        preferences={"color": "preto"},
        goal="discover",
    )
    updated, filled = rehydrate_interpretation_from_memories(interpretation, memories)
    assert updated.subject.brand == "Hamilton"  # current wins
    assert "brand" not in filled
    assert updated.preferences.budget_max == 4500
    assert updated.preferences.style == "clássico"
    assert updated.preferences.color == "preto"
    assert "budget_max" in filled
    assert "style" in filled


@pytest.mark.offline_eval
def test_golden_explicit_no_brand_blocks_certina_rehydrate():
    """brand_preference=Certina must lose to explicit_no:brand in contact memory."""
    memories = [
        ContactMemory(
            id=1,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="brand_preference",
            memory_kind="brand_preference",
            value={"brands": ["Certina"], "active": "Certina"},
            safe_summary="brand=Certina",
            use_in_instructions=True,
        ),
        ContactMemory(
            id=2,
            tenant_id="newstore",
            sender_key="whatsapp:1",
            memory_key="explicit_no:brand",
            memory_kind="explicit_no_preference",
            value={"value": "brand"},
            safe_summary="explicit_no=brand",
            use_in_instructions=True,
        ),
    ]
    interpretation = _interp(
        subject={},
        preferences={},
        goal="discover",
    )
    updated, filled = rehydrate_interpretation_from_memories(interpretation, memories)
    assert updated.subject.brand is None
    assert "brand" not in filled
    assert "brand" in list(updated.preferences.explicit_no_preferences or [])


def test_persist_contact_preferences_upserts_and_writes_summary(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory.contact_memory_repository as mem_repo
    import app.memory.contact_preference_memory as module
    import app.memory.conversation_summary_repository as summary_repo

    settings = SimpleNamespace(
        agent_contact_preference_memory_enabled=True,
        agent_contact_preference_summary_enabled=True,
        agent_contact_preference_rehydrate_enabled=True,
        agent_contact_preference_ttl_days=60,
        agent_contact_theme_ttl_days=30,
        agent_contact_preference_min_confidence=0.7,
        agent_max_conversation_summary_chars=2500,
        agent_max_active_contact_memories=20,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "upsert_contact_memory", mem_repo.upsert_contact_memory)
    monkeypatch.setattr(
        module,
        "get_active_contact_memories",
        mem_repo.get_active_contact_memories,
    )

    summary_calls: list[dict] = []

    def fake_apply_summary_delta(**kwargs):
        summary_calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(summary_repo, "apply_summary_delta", fake_apply_summary_delta)
    monkeypatch.setattr(
        "app.memory.memory_consolidation.consolidate_contact_memories",
        lambda **kwargs: 0,
    )

    interpretation = _interp(
        goal="recommend",
        subject={"brand": "Bulova"},
        preferences={"budget_max": 4500, "style": "esportivo"},
        enough_information_to_search=True,
        ready_for_retrieval=True,
        confidence=0.95,
    )
    result = persist_contact_preferences_from_interpretation(
        tenant_id="newstore",
        sender_key="whatsapp:5543996717931",
        conversation_key="whatsapp:5543996717931",
        interpretation=interpretation,
        inbound_id=99,
    )
    assert result["upserted"] >= 3
    assert "brand_preference" in result["keys"]
    assert result["summary_written"] is True
    assert summary_calls
    assert store.memories
    brand_rows = [
        row
        for row in store.memories
        if row["memory_key"] == "brand_preference" and row["status"] == "active"
    ]
    assert brand_rows
    assert brand_rows[0]["expires_at"] is not None
    assert brand_rows[0]["expires_at"] > datetime.now(timezone.utc) + timedelta(days=20)


def test_greeting_domain_includes_prior_commerce_theme(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory.contact_memory_repository as repo
    from app.memory.contact_memory_repository import select_relevant_memories

    store.memories.extend(
        [
            {
                "id": 1,
                "tenant_id": "newstore",
                "sender_key": "whatsapp:1",
                "memory_key": "last_commerce_theme",
                "memory_kind": "conversation_goal",
                "value": {"theme": "Bulova, clássico"},
                "safe_summary": "theme=Bulova, clássico",
                "status": "active",
                "importance": 0.95,
                "confidence": 0.9,
                "use_in_instructions": True,
                "sensitive": False,
            },
            {
                "id": 2,
                "tenant_id": "newstore",
                "sender_key": "whatsapp:1",
                "memory_key": "preferred_name",
                "memory_kind": "preferred_name",
                "value": {"value": "João"},
                "safe_summary": "João",
                "status": "active",
                "importance": 0.8,
                "confidence": 0.9,
                "use_in_instructions": True,
                "sensitive": False,
            },
        ]
    )
    monkeypatch.setattr(
        repo,
        "get_active_contact_memories",
        lambda **kwargs: [
            ContactMemory.model_validate(row)
            for row in store.memories
            if row["status"] == "active"
        ],
    )
    selected = select_relevant_memories(
        tenant_id="newstore",
        sender_key="whatsapp:1",
        domain="greeting",
        limit=5,
    )
    keys = {item.memory_key for item in selected}
    assert "last_commerce_theme" in keys
    assert "preferred_name" in keys
