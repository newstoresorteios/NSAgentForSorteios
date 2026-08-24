from app.handoff_service import (
    build_human_handoff_result,
    enrich_handoff_metadata,
    handoff_provider_payload,
    should_request_human_handoff,
)
from app.models import AgentResult, IncomingMessage


def test_customer_request_triggers_handoff():
    incoming = IncomingMessage(
        channel="whatsapp",
        text="Quero falar com um atendente",
    )
    assert should_request_human_handoff(incoming) == "customer_requested_human"
    result = build_human_handoff_result(reason="customer_requested_human")
    assert result.handoff_required is True
    assert "instantes" in result.reply_text.lower()
    assert "encaminh" in result.reply_text.lower()
    assert handoff_provider_payload(result)["provider_action"] == "mark_for_human"


def test_por_favor_accepts_joao_handoff_offer():
    from app.handoff_service import is_handoff_acceptance

    recent = [
        {
            "role": "assistant",
            "content": (
                "No momento não consegui acessar as fotos oficiais desses modelos "
                "por aqui. Se você quiser, eu posso passar para o João da equipe "
                "verificar isso com você."
            ),
        }
    ]
    assert is_handoff_acceptance("por favor", recent) is True
    incoming = IncomingMessage(channel="whatsapp", text="por favor")
    assert (
        should_request_human_handoff(incoming, recent_turns=recent)
        == "customer_accepted_handoff_offer"
    )
    result = build_human_handoff_result(reason="customer_accepted_handoff_offer")
    assert result.handoff_required is True
    assert "instantes" in result.reply_text.lower()
    assert "encaminh" in result.reply_text.lower()


def test_enrich_handoff_keeps_existing_reason():
    incoming = IncomingMessage(channel="instagram", text="ok")
    result = AgentResult(
        reply_text="Encaminhando",
        intent="handoff",
        handoff_required=True,
        safety_reason="blocked_topic:apostar",
    )
    enriched = enrich_handoff_metadata(incoming, result)
    assert enriched.response_metadata["handoff"]["reason"] == "blocked_topic:apostar"
    assert enriched.response_metadata["handoff"]["channel"] == "instagram"
