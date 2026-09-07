from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.llm.prompt_compiler as compiler
from app.models import IncomingMessage
from app.persona.store_knowledge import (
    fetch_institutional_knowledge,
    format_institutional_knowledge_block,
)


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


def test_fetch_institutional_knowledge_matches_pix_cues():
    package = fetch_institutional_knowledge("Posso pagar no PIX?")
    titles = [item["title"] for item in package.as_relevant_knowledge()]
    assert "Pagamento e PIX" in titles
    bodies = [item["body"] for item in package.as_relevant_knowledge()]
    assert any("não invente chave pix" in body.casefold() for body in bodies)
    assert any("não reabra atendimento humano" in body.casefold() for body in bodies)


def test_format_institutional_knowledge_block_is_empty_without_cues():
    assert format_institutional_knowledge_block("oi, tudo bem?") == ""


def test_format_institutional_knowledge_block_wraps_pix():
    block = format_institutional_knowledge_block("quero o link de pagamento")
    assert "<retrieved_knowledge>" in block
    assert "Pagamento e PIX" in block


@pytest.mark.offline_eval
@pytest.mark.asyncio
async def test_interpreter_prompt_includes_institutional_knowledge(monkeypatch):
    import app.sales_agent as sales_agent
    from tests.llm.openai_test_utils import install_fake_openai_client

    captured: dict = {}

    class FakeCompletions:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            from app.models import SalesInterpretation

            message = SimpleNamespace(
                parsed=SalesInterpretation(
                    domain="commerce",
                    goal="buy",
                    subject={"product_type": "relógio"},
                    preferences={},
                    references_previous_context=True,
                    needs_clarification=False,
                    confidence=0.9,
                ),
                refusal=None,
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="test-key",
            openai_model="gpt-4.1-mini",
            openai_main_model="gpt-4.1-mini",
            openai_fast_model="gpt-4.1-nano",
            agent_turn_understanding_enabled=False,
        ),
    )
    install_fake_openai_client(monkeypatch, FakeClient)

    await sales_agent.interpret_message(
        IncomingMessage(text="Posso pagar no PIX?"),
    )

    system_msg = captured["messages"][0]["content"]
    assert "<retrieved_knowledge>" in system_msg
    assert "Pagamento e PIX" in system_msg
    assert "Não invente chave PIX" in system_msg
