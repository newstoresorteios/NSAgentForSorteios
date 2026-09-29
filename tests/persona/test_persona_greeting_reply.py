from __future__ import annotations

import pytest

from app.models import IncomingMessage


@pytest.mark.asyncio
async def test_persona_greeting_uses_compiled_crono_prompt(monkeypatch):
    import app.openai_agent as agent

    monkeypatch.setattr(
        agent,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "agent_db_persona_enabled": True,
                "openai_api_key": "sk-test",
                "openai_model": "gpt-test",
                "max_reply_chars": 500,
            },
        )(),
    )
    monkeypatch.setattr(
        agent,
        "gather_customer_facts",
        lambda *_a, **_k: {"found": False, "primary_intent": "greeting"},
    )
    monkeypatch.setattr(
        agent,
        "build_agent_input",
        lambda *_a, **_k: "Bom dia",
    )

    captured: dict = {}

    def _fake_resolve(**kwargs):
        captured["fallback"] = kwargs.get("fallback_instructions")
        return "<user_managed_persona>\nCrono New Store\n</user_managed_persona>\n" + str(
            kwargs.get("fallback_instructions") or ""
        )

    monkeypatch.setattr(
        "app.llm.prompt_compiler.resolve_system_instructions",
        _fake_resolve,
    )

    class _Text:
        text = "Olá! Eu sou o Crono, assistente virtual da New Store Relógios. Como posso te ajudar hoje?"

    async def _fake_generate(**kwargs):
        captured["messages"] = kwargs.get("messages")
        return _Text()

    monkeypatch.setattr(
        "app.llm.openai_gateway.generate_text_output",
        _fake_generate,
    )

    incoming = IncomingMessage(channel="whatsapp", text="Bom dia", sender_key="wa:1")
    result = await agent.generate_persona_greeting_reply(incoming, {"found": False})
    assert result.safety_reason is None
    assert "Crono" in result.reply_text
    assert "greeting_contract" in str(captured.get("fallback") or "")
    system = captured["messages"][0]["content"]
    assert "Crono New Store" in system


@pytest.mark.asyncio
async def test_repeated_persona_text_greeting_uses_compiled_persona(monkeypatch):
    import app.agents.door as door
    from app.commerce.commerce_context import CommerceConversationState
    from app.persona.persona_runtime import (
        PersonaRuntimeConfig,
        reset_persona_runtime,
        set_persona_runtime,
    )

    official = (
        "Olá! Eu sou o Crono, assistente virtual da New Store Relógios. "
        "Como posso te ajudar hoje?"
    )
    runtime = PersonaRuntimeConfig(
        loaded=True,
        enabled=True,
        agent_display_name="Crono",
        greeting_text=official,
        greeting_mode="persona_text",
    )
    monkeypatch.setattr(
        door,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"agent_db_persona_enabled": True, "openai_api_key": "sk-test"},
        )(),
    )

    async def _persona_reply(*_args, **_kwargs):
        from app.models import AgentResult

        return AgentResult(
            reply_text="Oi de novo! Estou por aqui — me conta como posso ajudar.",
            intent="general",
        )

    monkeypatch.setattr(door, "generate_persona_greeting_reply", _persona_reply)
    token = set_persona_runtime(runtime)
    try:
        result = await door._resolve_greeting_door(
            message=IncomingMessage(channel="instagram", text="olá", sender_key="ig:1"),
            customer_context={"found": False},
            recent_turns=[{"role": "assistant", "content": official}],
            commerce_state=CommerceConversationState(),
            phrase_restart=False,
        )
    finally:
        reset_persona_runtime(token)

    assert result is not None
    assert result.reply_text == "Oi de novo! Estou por aqui — me conta como posso ajudar."
    assert result.response_metadata["used_openai_responder"] is True
    assert result.response_metadata["persona_applied_to_output"] is True
    assert result.response_metadata["persona_render_path"] == "compiled_persona_greeting"
