from __future__ import annotations

from types import SimpleNamespace

import app.persona_repository as repo
import app.prompt_compiler as compiler
from app.models import IncomingMessage
from tests.persona_fakes import InMemoryPersonaStore


def _enable_persona(monkeypatch, enabled: bool = True):
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=enabled,
            agent_prompt_compilation_audit_enabled=True,
            agent_debug_store_compiled_prompt=False,
            agent_max_recent_turns=8,
            openai_api_mode="chat_completions",
            agent_persona_tenant_id="newstore",
            agent_persona_key="newstore_commercial",
        ),
    )


def test_resolve_passthrough_when_flag_disabled(monkeypatch):
    _enable_persona(monkeypatch, enabled=False)
    fallback = "fallback instructions in code"
    assert (
        compiler.resolve_system_instructions(fallback_instructions=fallback)
        == fallback
    )


def test_compile_includes_active_persona_and_safety(monkeypatch):
    store = InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(
        instructions="PERSONA_ATIVA_NEWSTORE\n",
        name="NS",
    )
    repo.activate_persona_version(created.id)

    incoming = IncomingMessage(channel="instagram", text="oi", sender_key="ig:1")
    compiled = compiler.compile_agent_prompt(
        incoming=incoming,
        fallback_instructions="fallback",
        audit=True,
    )
    assert compiled.used_db_persona is True
    assert compiled.persona_version_id == created.id
    assert "PERSONA_ATIVA_NEWSTORE" in compiled.instructions
    assert "<fixed_safety_policy>" in compiled.instructions
    assert "<channel_overlay>" in compiled.instructions
    assert "instagram" in compiled.instructions
    assert len(store.compilations) == 1


def test_compile_recomputes_each_call_without_openai_state(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(instructions="P1\n", name="NS")
    repo.activate_persona_version(created.id)

    a = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="a"),
        fallback_instructions="fb",
        audit=False,
    )
    b = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="b"),
        fallback_instructions="fb",
        audit=False,
    )
    assert a.instructions_hash == b.instructions_hash
    assert a.input_items[-1]["content"] == "a"
    assert b.input_items[-1]["content"] == "b"


def test_tenant_isolation(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    a = repo.create_persona_version(
        instructions="TENANT_A\n",
        tenant_id="tenant_a",
        name="A",
    )
    repo.activate_persona_version(a.id, tenant_id="tenant_a")
    b = repo.create_persona_version(
        instructions="TENANT_B\n",
        tenant_id="tenant_b",
        name="B",
    )
    repo.activate_persona_version(b.id, tenant_id="tenant_b")

    compiled_a = compiler.compile_agent_prompt(
        tenant_id="tenant_a",
        fallback_instructions="fb",
        audit=False,
    )
    compiled_b = compiler.compile_agent_prompt(
        tenant_id="tenant_b",
        fallback_instructions="fb",
        audit=False,
    )
    assert "TENANT_A" in compiled_a.instructions
    assert "TENANT_B" not in compiled_a.instructions
    assert "TENANT_B" in compiled_b.instructions
    assert "TENANT_A" not in compiled_b.instructions
