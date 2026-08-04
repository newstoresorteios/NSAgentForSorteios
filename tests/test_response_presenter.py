from app.models import AgentResult, IncomingMessage
from app.response_composer import compose_outbound_reply
from app.response_presenter import present_reply_text


def test_strips_generic_opener_and_limits_questions():
    text = present_reply_text(
        "Claro! O Seastar custa R$ 10,00. Quer que eu reserve? Posso preparar o frete também?",
        channel="whatsapp",
        intent="commerce",
    )
    assert not text.lower().startswith("claro")
    assert text.count("?") <= 1


def test_marks_similar_product_and_preserves_url():
    text = present_reply_text(
        "Achei este modelo. https://www.sorteionewstore.com.br/produto/1",
        channel="whatsapp",
        intent="commerce",
        metadata={"match_kind": "similar"},
    )
    assert "semelhante" in text.casefold()
    assert "https://www.sorteionewstore.com.br/produto/1" in text


def test_compose_greeting_handoff_and_long_message():
    greeting = compose_outbound_reply(
        IncomingMessage(channel="whatsapp", text="oi"),
        AgentResult(reply_text="Com certeza! Olá!", intent="greeting"),
    )
    assert "Com certeza" not in greeting.reply_text

    handoff = compose_outbound_reply(
        IncomingMessage(channel="whatsapp", text="atendente"),
        AgentResult(
            reply_text="Vou te transferir. Qual horário prefere? Quer pix também?",
            intent="handoff",
            handoff_required=True,
        ),
    )
    assert handoff.reply_text.count("?") == 0

    long = compose_outbound_reply(
        IncomingMessage(channel="instagram", text="busca"),
        AgentResult(
            reply_text="\n\n".join([f"Bloco {i}" for i in range(6)]),
            intent="commerce",
        ),
        max_reply_chars=700,
    )
    assert long.reply_text.count("\n\n") <= 1
    assert len(long.reply_text) <= 700


def test_audio_disabled_on_instagram_profile():
    result = compose_outbound_reply(
        IncomingMessage(channel="instagram", text="oi"),
        AgentResult(
            reply_text="Olá",
            intent="greeting",
            reply_modality="audio",
            reply_audio_url="https://example.com/a.ogg",
        ),
    )
    assert result.reply_modality == "text"
    assert result.reply_audio_url is None
