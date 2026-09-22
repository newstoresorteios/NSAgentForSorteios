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


@pytest.mark.parametrize(
    ("question", "expected_slug", "expected_fact"),
    [
        (
            "Tenho quantos dias para devolver o relógio?",
            "trocas-e-devolucoes",
            "30 dias corridos",
        ),
        (
            "Qual o prazo de uma peça sob encomenda?",
            "frete-e-entrega",
            "25 a 35 dias úteis",
        ),
        (
            "Os produtos são originais e têm procedência?",
            "faq-comercial",
            "produtos originais",
        ),
        (
            "Posso pagar no cartão e parcelar?",
            "pagamento-e-compra-segura",
            "checkout oficial",
        ),
        (
            "A garantia cobre bateria e vidro?",
            "garantia-e-cuidados",
            "Não são cobertos bateria, vidro",
        ),
    ],
)
def test_official_policy_documents_are_retrieved_by_subject(
    question, expected_slug, expected_fact
):
    items = fetch_institutional_knowledge(question).as_relevant_knowledge()

    document = next(item for item in items if item.get("slug") == expected_slug)
    assert expected_fact in document["body"]
    assert document["source_url"].startswith("https://www.newstorerj.com.br/")


def test_official_policy_source_is_rendered_in_prompt():
    block = format_institutional_knowledge_block(
        "Como funciona a devolução por arrependimento?"
    )

    assert "Fonte oficial: https://www.newstorerj.com.br/" in block
    assert "politica-de-troca-e-devolucao-new-store" in block


def test_fetch_institutional_knowledge_includes_persona_metadata():
    package = fetch_institutional_knowledge(
        "Como funciona a revisão técnica dos seminovos?",
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


def test_fetch_institutional_knowledge_omits_unrelated_persona_metadata():
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

    assert package.as_relevant_knowledge() == []


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
    assert "equipe humana" in instructions


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
