from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.models import IncomingMessage, SalesInterpretation


def _settings(*, api_key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(openai_api_key=api_key, openai_model="gpt-test", database_url="postgresql://test")


def _fake_openai(monkeypatch, interpretation: SalesInterpretation, captured: dict) -> None:
    class FakeCompletions:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(parsed=interpretation, refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.sales_agent.AsyncOpenAI", FakeClient)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_text", "history", "interpretation", "expected_style", "expected_budget"),
    [
        (
            "esportivo",
            [
                {"role": "user", "content": "quero comprar um relógio"},
                {"role": "assistant", "content": "Você procura algo esportivo, social ou casual?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="discover",
                subject={"product_type": "relógio"},
                preferences={"style": "esportivo"},
                references_previous_context=True,
                confidence=0.98,
            ),
            "esportivo",
            None,
        ),
        (
            "menos de 5 mil reais",
            [
                {"role": "user", "content": "quero um relógio esportivo"},
                {"role": "assistant", "content": "Qual faixa de preço você prefere?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="recommend",
                subject={"product_type": "relógio"},
                preferences={"style": "esportivo", "budget_max": 5000},
                references_previous_context=True,
                confidence=0.98,
            ),
            "esportivo",
            5000,
        ),
        (
            "social",
            [
                {"role": "user", "content": "me recomende relógios"},
                {"role": "assistant", "content": "Qual estilo você prefere?"},
            ],
            SalesInterpretation(
                domain="commerce",
                goal="recommend",
                subject={"product_type": "relógio"},
                preferences={"style": "social"},
                references_previous_context=True,
                confidence=0.97,
            ),
            "social",
            None,
        ),
    ],
)
async def test_interpreter_uses_recent_turns_for_short_followups(
    monkeypatch,
    current_text,
    history,
    interpretation,
    expected_style,
    expected_budget,
):
    import app.sales_agent as sales_agent

    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(
        IncomingMessage(text=current_text),
        recent_turns=history,
    )

    assert result.domain == "commerce"
    assert result.subject.product_type == "relógio"
    assert result.preferences.style == expected_style
    assert result.preferences.budget_max == expected_budget
    assert result.references_previous_context is True
    assert captured["messages"][1:-1] == history
    assert captured["messages"][-1] == {"role": "user", "content": current_text}
    assert captured["response_format"] is SalesInterpretation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "interpretation", "expected_domain"),
    [
        ("quem ganhou o jogo ontem?", SalesInterpretation(domain="out_of_scope", confidence=0.99), "out_of_scope"),
        ("como funciona o sorteio?", SalesInterpretation(domain="raffle", confidence=0.99), "raffle"),
        (
            "preciso de um relógio para dar de presente, não queria gastar muito",
            SalesInterpretation(
                domain="commerce",
                goal="discover",
                subject={"product_type": "relógio"},
                preferences={"occasion": "presente"},
                needs_clarification=True,
                clarification_question="Qual faixa de preço você tem em mente?",
                confidence=0.96,
            ),
            "commerce",
        ),
    ],
)
async def test_interpreter_returns_validated_domains(monkeypatch, text, interpretation, expected_domain):
    import app.sales_agent as sales_agent

    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(IncomingMessage(text=text))

    assert result.domain == expected_domain
    if result.needs_clarification:
        assert result.clarification_question


@pytest.mark.asyncio
async def test_greeting_uses_fast_local_interpretation_without_openai(monkeypatch):
    import app.sales_agent as sales_agent

    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        sales_agent,
        "AsyncOpenAI",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("greeting must not call OpenAI")),
    )

    result = await sales_agent.interpret_message(IncomingMessage(text="oi"))

    assert result.domain == "greeting"


@pytest.mark.asyncio
async def test_openai_is_attempted_before_deterministic_fallback(monkeypatch):
    import app.sales_agent as sales_agent

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={"style": "esportivo"},
        references_previous_context=True,
        confidence=0.95,
    )
    captured = {}
    monkeypatch.setattr(sales_agent, "get_settings", lambda: _settings())
    _fake_openai(monkeypatch, interpretation, captured)

    result = await sales_agent.interpret_message(
        IncomingMessage(text="esportivo"),
        recent_turns=[{"role": "user", "content": "quero um relógio"}],
    )

    assert captured
    assert result.domain == "commerce"
    assert result._source == "openai"


def test_load_recent_conversation_turns_prefers_conversation_and_delivered_replies(monkeypatch):
    import app.db as db

    captured = {}
    rows = [
        {"id": 12, "text": "menos de 5 mil", "reply_text": None},
        {"id": 10, "text": "quero comprar um relógio", "reply_text": "Qual estilo você prefere?"},
    ]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchall(self):
            return rows

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    @contextmanager
    def fake_get_conn():
        yield FakeConnection()

    monkeypatch.setattr(db, "get_settings", lambda: _settings())
    monkeypatch.setattr(db, "get_conn", fake_get_conn)

    turns = db.load_recent_conversation_turns(
        conversation_id="conversation-1",
        sender_phone="5511999999999",
        before_inbound_id=20,
        limit=8,
    )

    assert turns == [
        {"role": "user", "content": "quero comprar um relógio"},
        {"role": "assistant", "content": "Qual estilo você prefere?"},
        {"role": "user", "content": "menos de 5 mil"},
    ]
    assert captured["params"]["conversation_id"] == "conversation-1"
    assert "sender_phone" not in captured["params"]
    assert captured["params"]["before_inbound_id"] == 20
    assert "provider_send_ok = true" in captured["query"]
    assert "inbound.id < %(before_inbound_id)s" in captured["query"]


@pytest.mark.asyncio
async def test_async_agent_passes_loaded_history_to_interpreter(monkeypatch):
    import app.openai_agent as openai_agent
    from app.models import AgentResult

    history = [
        {"role": "user", "content": "quero comprar um relógio"},
        {"role": "assistant", "content": "Você prefere esportivo ou social?"},
    ]
    captured = {}
    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "relógio"},
        preferences={"style": "esportivo"},
        references_previous_context=True,
        needs_clarification=True,
        clarification_question="Qual faixa de preço você prefere?",
        confidence=0.98,
    )

    monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", lambda **kwargs: history)

    async def fake_interpret(message, *, recent_turns=None):
        captured["history"] = recent_turns
        return interpretation

    async def fake_handle(message, facts, customer_context, semantic_plan):
        captured["plan"] = semantic_plan
        return AgentResult(reply_text="Qual faixa de preço você prefere?", intent="commerce")

    monkeypatch.setattr(openai_agent, "interpret_message", fake_interpret)
    monkeypatch.setattr(openai_agent, "handle_sales_message", fake_handle)

    result = await openai_agent.generate_agent_reply_async(
        IncomingMessage(
            text="esportivo",
            conversation_id="conversation-1",
            sender_phone="5511999999999",
            raw={"inbound_id": 30},
        ),
        {},
    )

    assert result.intent == "commerce"
    assert captured["history"] == history
    assert captured["plan"] is interpretation
