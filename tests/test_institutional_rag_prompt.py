from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.prompt_compiler as compiler
from app.models import IncomingMessage
from app.store_knowledge import fetch_institutional_knowledge


def test_fetch_institutional_knowledge_matches_trade_in_cues():
    package = fetch_institutional_knowledge("Vocês compram relógio usado?")
    titles = [item["title"] for item in package.as_relevant_knowledge()]
    assert "Troca e avaliação" in titles


def test_fetch_institutional_knowledge_includes_persona_metadata():
    package = fetch_institutional_knowledge(
        "oi",
        persona_metadata={
            "institutionalKnowledge": [
                {
                    "title": "Política loja",
                    "body": "Seminovos passam por revisão técnica antes da venda.",
                }
            ]
        },
    )
    bodies = [item["body"] for item in package.as_relevant_knowledge()]
    assert any("revisão técnica" in body for body in bodies)


@pytest.mark.offline_eval
def test_resolve_system_instructions_includes_knowledge_snippet(monkeypatch):
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=False,
            agent_contact_memory_in_prompt_enabled=False,
            agent_memory_auto_apply_enabled=False,
            agent_persona_tenant_id="newstore",
            agent_persona_key="newstore_commercial",
        ),
    )
    incoming = IncomingMessage(
        channel="whatsapp",
        text="Vocês avaliam relógio seminovo para troca?",
        sender_phone="5511999999999",
    )
    instructions = compiler.resolve_system_instructions(
        fallback_instructions="fallback base",
        incoming=incoming,
    )
    assert "fallback base" in instructions
    assert "<retrieved_knowledge>" in instructions
    assert "Troca e avaliação" in instructions
    assert "não inventar valores de avaliação" in instructions
