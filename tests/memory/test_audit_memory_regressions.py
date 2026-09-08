from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.memory.memory_models import ContactMemory, MemoryProposal
from app.memory.memory_policy import evaluate_memory_proposal
from app.memory.context_resume import merge_commerce_states
from app.memory.contact_preference_memory import (
    persist_contact_preferences_from_interpretation, rehydrate_interpretation_from_memories,
)
from app.models import IncomingMessage, SalesInterpretation
from tests.memory.memory_fakes import InMemoryMemoryStore


def settings():
    return SimpleNamespace(agent_memory_auto_apply_enabled=True,
        agent_memory_auto_apply_sender_allowlist="*",
        agent_contact_preference_memory_enabled=True,
        agent_contact_preference_summary_enabled=False)


def memory(key, value, kind="brand_preference"):
    return ContactMemory(id=1, tenant_id="audit", sender_key="audit", memory_key=key,
                         memory_kind=kind, value=value)


@pytest.mark.parametrize("value", ["Citizen", None])
def test_forget_accepts_existing_value_or_no_value(value):
    proposal = MemoryProposal(action="forget", kind="brand_preference", key="preferred_brands",
        value=value, reason_code="explicit_user_forget_request")
    with patch("app.memory.memory_policy.get_settings", settings):
        decision = evaluate_memory_proposal(proposal=proposal, sender_key="audit",
            current_memories=[memory("preferred_brands", {"value": "Citizen"})])
    assert decision.accepted and decision.auto_apply


@pytest.mark.parametrize("key", ["explicit_no:brand", "explicit_no_preference_brand"])
def test_explicit_new_brand_replaces_previous_no_preference(monkeypatch, key):
    store = InMemoryMemoryStore().install(monkeypatch)
    import app.memory.contact_memory_repository as repo
    import app.memory.contact_preference_memory as module
    monkeypatch.setattr(module, "get_settings", settings)
    monkeypatch.setattr(module, "upsert_contact_memory", repo.upsert_contact_memory)
    monkeypatch.setattr(module, "get_active_contact_memories", repo.get_active_contact_memories)
    monkeypatch.setattr("app.memory.memory_consolidation.consolidate_contact_memories", lambda **kwargs: None)
    repo.upsert_contact_memory(tenant_id="audit", sender_key="audit", memory_key=key,
        memory_kind="explicit_no_preference", value={"value": "brand"})
    incoming = SalesInterpretation(domain="commerce", goal="find", confidence=0.99, needs_clarification=False,
        subject={"brand": "Seiko"}, references_previous_context=False)
    old = repo.get_active_contact_memories(tenant_id="audit", sender_key="audit")
    hydrated, _ = rehydrate_interpretation_from_memories(incoming, old, message_text="Agora prefiro Seiko")
    assert "brand" not in hydrated.preferences.explicit_no_preferences
    persist_contact_preferences_from_interpretation(interpretation=hydrated,
        tenant_id="audit", sender_key="audit", conversation_key=None, message_text="Agora prefiro Seiko")
    active = repo.get_active_contact_memories(tenant_id="audit", sender_key="audit")
    assert key not in {m.memory_key for m in active}
    assert next(m for m in active if m.memory_key == "brand_preference").value["active"] == "Seiko"


def test_brand_proposal_requires_customer_evidence_to_replace_no_preference():
    proposal = MemoryProposal(action="upsert", kind="brand_preference", key="preferred_brands",
        value="Seiko", reason_code="explicit_user_correction", confidence=0.99, importance=0.99)
    old = [memory("explicit_no:brand", {"value": "brand"}, "explicit_no_preference")]
    for text, accepted in [("Agora prefiro Seiko", True), ("Não quero Seiko", False), ("Pode mostrar mais", False)]:
        decision = evaluate_memory_proposal(proposal=proposal, current_memories=old,
            inbound=IncomingMessage(channel="whatsapp", text=text))
        assert decision.accepted is accepted


def test_brand_proposal_auto_apply_clears_old_no_preference(monkeypatch):
    InMemoryMemoryStore().install(monkeypatch)
    import app.memory.contact_memory_repository as repo
    from app.memory.memory_models import AgentTurnEnvelope
    from app.memory.memory_service import process_agent_memory_proposals
    monkeypatch.setattr("app.memory.memory_policy.get_settings", settings)
    monkeypatch.setattr("app.memory.memory_service.get_settings", lambda: SimpleNamespace(
        **vars(settings()), agent_memory_proposals_enabled=True))
    monkeypatch.setattr("app.memory.memory_consolidation.consolidate_contact_memories", lambda **kwargs: None)
    repo.upsert_contact_memory(tenant_id="audit", sender_key="audit", memory_key="explicit_no:brand",
        memory_kind="explicit_no_preference", value={"value": "brand"})
    proposal = MemoryProposal(action="upsert", kind="brand_preference", key="preferred_brands",
        value="Seiko", reason_code="explicit_user_correction", confidence=0.99, importance=0.99)
    result = process_agent_memory_proposals(envelope=AgentTurnEnvelope(reply="Ok", memory_proposals=[proposal]),
        tenant_id="audit", sender_key="audit", conversation_key="audit",
        inbound=IncomingMessage(channel="whatsapp", text="Agora prefiro Seiko"))
    assert result.proposals_applied == 1
    active = repo.get_active_contact_memories(tenant_id="audit", sender_key="audit")
    assert "explicit_no:brand" not in {item.memory_key for item in active}
    assert next(item for item in active if item.memory_key == "preferred_brands").value == {"value": "Seiko"}


def test_state_merge_preserves_new_budget_brand_phase_and_cleared_shortlist():
    latest = {"active_preferences": {"budget_max": 2500, "brand": "Seiko"},
              "last_presented_products": [], "dialogue_phase": "discovery"}
    donor = {"order_id": "old-order", "active_preferences": {"budget_max": 9000},
             "last_presented_products": [{"product_id": "old"}], "dialogue_phase": "checkout"}
    merged = merge_commerce_states(latest, donor)
    assert merged["active_preferences"] == latest["active_preferences"]
    assert merged["last_presented_products"] == []
    assert merged["dialogue_phase"] == "discovery"
    assert merged["order_id"] == "old-order"


def test_state_merge_never_mixes_payment_url_from_another_order():
    merged = merge_commerce_states({"order_id": "new"},
        {"order_id": "old", "order_payment_url": "https://example.test/old"})
    assert not merged.get("order_payment_url")


def test_state_merge_without_latest_still_recovers_fallback():
    previous = {"last_presented_products": [{"product_id": "1"}], "active_preferences": {"budget_max": 2500}}
    assert merge_commerce_states({}, previous) == previous


@pytest.mark.parametrize("value", ["estoque disponível", "status do pedido aprovado", "frete grátis", "R$ 2500 estoque disponível"])
def test_price_preference_cannot_bypass_volatile_fact_filter(value):
    proposal = MemoryProposal(action="upsert", scope="contact", kind="price_preference",
        key="preferred_price_max", value=value,
        confidence=0.99, importance=0.99, reason_code="explicit_user_preference")
    with patch("app.memory.memory_policy.get_settings", settings):
        decision = evaluate_memory_proposal(proposal=proposal, sender_key="audit")
    assert not decision.accepted and "commercial_volatile" in decision.rejection_codes


def test_price_preference_still_accepts_currency_budget():
    proposal = MemoryProposal(action="upsert", kind="price_preference",
        key="preferred_price_max", value="até R$ 2500")
    assert evaluate_memory_proposal(proposal=proposal).accepted


def test_preferred_brands_alias_rehydrates_as_brand():
    memories = [memory("preferred_brands", {"value": "Seiko"})]
    incoming = SalesInterpretation(domain="commerce", goal="find", confidence=0.9,
        needs_clarification=False, subject={}, references_previous_context=True)
    hydrated, filled = rehydrate_interpretation_from_memories(
        incoming, memories, message_text="pode continuar nessa linha"
    )
    assert hydrated.subject.brand == "Seiko"
    assert "brand" in filled


def test_cas_conflict_is_not_marked_applied(monkeypatch):
    from app.memory.memory_models import AgentTurnEnvelope, ConversationSummaryDelta
    from app.memory.memory_service import process_agent_memory_proposals

    calls = {"applied": 0, "rejected": []}

    def conflict(**_kwargs):
        return {"cas_conflict": True, "summary": "stale"}

    monkeypatch.setattr(
        "app.memory.memory_service.get_settings",
        lambda: SimpleNamespace(
            agent_memory_proposals_enabled=False,
            agent_instruction_extension_proposals_enabled=False,
            agent_conversation_summary_mode="enforce",
            agent_conversation_summary_enabled=True,
            agent_max_conversation_summary_chars=2500,
        ),
    )
    monkeypatch.setattr(
        "app.memory.conversation_summary_repository.get_conversation_summary",
        lambda **_k: {"version": 3, "current_goal": "old"},
    )
    monkeypatch.setattr("app.memory.memory_service.apply_summary_delta", conflict)
    monkeypatch.setattr(
        "app.memory.memory_service.insert_memory_proposal",
        lambda **_k: 41,
    )
    monkeypatch.setattr(
        "app.memory.memory_service.mark_proposal_applied",
        lambda *_a, **_k: calls.__setitem__("applied", calls["applied"] + 1),
    )
    monkeypatch.setattr(
        "app.memory.memory_service.mark_proposal_rejected",
        lambda _id, rejection_codes=None: calls["rejected"].extend(rejection_codes or []),
    )

    result = process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(
            reply="ok",
            conversation_summary_delta=ConversationSummaryDelta(
                current_goal="escolher relógio",
                commitments=["avisar quando chegar"],
            ),
        ),
        tenant_id="audit",
        sender_key="audit",
        conversation_key="conv-1",
    )
    assert calls["applied"] == 0
    assert result.proposals_applied == 0
    assert "cas_conflict" in result.rejection_codes


def test_conversation_scope_does_not_write_contact_memory(monkeypatch):
    from app.memory.memory_models import AgentTurnEnvelope
    from app.memory.memory_service import process_agent_memory_proposals

    writes = []
    monkeypatch.setattr("app.memory.memory_policy.get_settings", settings)
    monkeypatch.setattr(
        "app.memory.memory_service.get_settings",
        lambda: SimpleNamespace(
            **vars(settings()),
            agent_memory_proposals_enabled=True,
            agent_conversation_summary_mode="off",
            agent_instruction_extension_proposals_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "app.memory.memory_service.get_active_contact_memories",
        lambda **_k: [],
    )
    monkeypatch.setattr(
        "app.memory.memory_service.insert_memory_proposal",
        lambda **_k: 7,
    )
    monkeypatch.setattr(
        "app.memory.memory_service.upsert_contact_memory",
        lambda **kwargs: writes.append(kwargs) or memory("x", {}),
    )
    monkeypatch.setattr(
        "app.memory.memory_service.mark_proposal_applied",
        lambda *_a, **_k: None,
    )

    result = process_agent_memory_proposals(
        envelope=AgentTurnEnvelope(
            reply="ok",
            memory_proposals=[
                MemoryProposal(
                    action="upsert",
                    scope="conversation",
                    kind="conversation_goal",
                    key="current_goal",
                    value="fechar o Seiko",
                    reason_code="explicit_user_preference",
                    confidence=0.99,
                    importance=0.99,
                )
            ],
        ),
        tenant_id="audit",
        sender_key="audit",
        conversation_key="conv-a",
        inbound=IncomingMessage(channel="whatsapp", text="quero fechar o Seiko"),
    )
    assert writes == []
    assert result.proposals_applied == 0


def test_recipient_requires_conversation_scope():
    contact = MemoryProposal(
        action="upsert",
        scope="contact",
        kind="recipient",
        key="recipient",
        value="apelido não confirmado",
        reason_code="explicit_user_identity",
        confidence=0.99,
        importance=0.99,
    )
    rejected = evaluate_memory_proposal(proposal=contact, sender_key="audit")
    assert rejected.accepted is False
    assert "conversation_scope_required" in rejected.rejection_codes

    conversation = contact.model_copy(update={"scope": "conversation"})
    accepted = evaluate_memory_proposal(proposal=conversation, sender_key="audit")
    assert accepted.accepted is True
    assert accepted.proposal_type == "conversation_memory"


def test_deterministic_contact_preferences_do_not_persist_recipient():
    from app.memory.contact_preference_memory import build_preference_memory_items

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        confidence=0.99,
        needs_clarification=False,
        references_previous_context=False,
        preferences={"recipient": "apelido não confirmado", "budget_max": 2500},
    )
    items = build_preference_memory_items(interpretation)
    assert "recipient" not in {item["memory_key"] for item in items}


def test_legacy_recipient_memory_never_rehydrates_another_thread():
    incoming = SalesInterpretation(
        domain="commerce",
        goal="find",
        confidence=0.9,
        needs_clarification=False,
        references_previous_context=False,
    )
    legacy = memory("recipient", {"value": "apelido não confirmado"}, "recipient")
    hydrated, filled = rehydrate_interpretation_from_memories(
        incoming,
        [legacy],
        message_text="quero um relógio",
    )
    assert hydrated.preferences.recipient is None
    assert "recipient" not in filled
