from __future__ import annotations

from types import SimpleNamespace

from app.contact_preference_memory import (
    build_preference_memory_items,
    persist_contact_preferences_from_interpretation,
)
from app.models import SalesInterpretation
from tests.memory_fakes import InMemoryMemoryStore


def test_build_preference_memory_items_from_bulova_turn():
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={"product_type": "relógio", "brand": "Bulova"},
        preferences={
            "color": "dourado",
            "material": "automático",
            "budget_max": 5000,
            "style": "clássico",
        },
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
        confidence=0.9,
    )
    items = build_preference_memory_items(interpretation)
    keys = {item["memory_key"] for item in items}
    assert "brand_preference" in keys
    assert "color_preference" in keys
    assert "style_preference" in keys
    assert "price_preference" in keys
    assert "last_commerce_theme" in keys
    theme = next(item for item in items if item["memory_key"] == "last_commerce_theme")
    assert "Bulova" in theme["safe_summary"]
    assert "clássico" in theme["safe_summary"]


def test_persist_contact_preferences_upserts_and_writes_summary(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.contact_memory_repository as mem_repo
    import app.contact_preference_memory as module
    import app.conversation_summary_repository as summary_repo

    settings = SimpleNamespace(
        agent_contact_preference_memory_enabled=True,
        agent_contact_preference_summary_enabled=True,
        agent_max_conversation_summary_chars=2500,
        agent_max_active_contact_memories=20,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    # Prefer the in-memory upsert installed on the repository module.
    monkeypatch.setattr(module, "upsert_contact_memory", mem_repo.upsert_contact_memory)

    summary_calls: list[dict] = []

    def fake_apply_summary_delta(**kwargs):
        summary_calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(summary_repo, "apply_summary_delta", fake_apply_summary_delta)
    monkeypatch.setattr(
        "app.memory_consolidation.consolidate_contact_memories",
        lambda **kwargs: 0,
    )

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="recommend",
        subject={"brand": "Bulova"},
        preferences={"budget_max": 4500, "style": "esportivo"},
        references_previous_context=False,
        enough_information_to_search=True,
        ready_for_retrieval=True,
        needs_clarification=False,
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
    kinds = {row["memory_kind"] for row in store.memories}
    assert "brand_preference" in kinds
    assert "conversation_goal" in kinds


def test_greeting_domain_includes_prior_commerce_theme(monkeypatch):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.contact_memory_repository as repo
    from app.contact_memory_repository import select_relevant_memories
    from app.memory_models import ContactMemory

    store.memories.extend(
        [
            {
                "id": 1,
                "tenant_id": "newstore",
                "sender_key": "whatsapp:1",
                "memory_key": "last_commerce_theme",
                "memory_kind": "conversation_goal",
                "value": {"theme": "Bulova, clássico"},
                "safe_summary": "Último tema de interesse: Bulova, clássico",
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
