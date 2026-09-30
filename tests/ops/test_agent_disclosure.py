import pytest

from app.identity.agent_disclosure import apply_agent_disclosure
from app.ingress.outbox import build_outbound_envelope, result_from_outbox_row
from app.models import AgentResult, IncomingMessage
from app.ops.handoff_service import build_human_handoff_result, enrich_handoff_metadata, ensure_handoff_queued
from app.persona.persona_runtime import PersonaRuntimeConfig, set_persona_runtime, reset_persona_runtime


def test_identity_is_workspace_scoped_and_retry_is_immutable():
    incoming = IncomingMessage(channel="instagram", text="Qual valor?")
    token = set_persona_runtime(PersonaRuntimeConfig(loaded=True, enabled=True,
        workspace_id="shop-a", agent_display_name="Crono",
        greeting_text="Sou Crono, assistente virtual da NewStore."))
    try:
        result = apply_agent_disclosure(AgentResult(reply_text="R$ 99,00 — https://example.com/item", intent="commerce"), incoming=incoming)
        assert result.reply_text.startswith("Olá! Sou Crono, assistente virtual (IA) da NewStore.")
        result.reply_audio_bytes = b"\xff\x00"
        result.response_metadata["private_prompt"] = "must not persist"
        envelope = build_outbound_envelope(incoming, result)
    finally:
        reset_persona_runtime(token)
    rebuilt = result_from_outbox_row({"reply_payload": envelope, "reply_text": result.reply_text})
    assert apply_agent_disclosure(rebuilt).reply_text == result.reply_text
    assert "private_prompt" not in rebuilt.response_metadata
    assert "reply_audio_bytes" not in envelope["result"]
    assert rebuilt.response_metadata["reply_audio_size"] == 2


def test_fallback_does_not_invent_company_and_human_is_untouched():
    token = set_persona_runtime(None)
    try:
        result = apply_agent_disclosure(AgentResult(reply_text="Boa tarde!", intent="commerce"))
        assert result.reply_text == "Assistente virtual (IA)\n\nBoa tarde!"
        human = AgentResult(reply_text="Felipe: Olá!", intent="commerce", response_metadata={"actor_type": "human"})
        assert apply_agent_disclosure(human).reply_text == "Felipe: Olá!"
    finally:
        reset_persona_runtime(token)


def test_visual_failure_explains_attempt_and_only_offers_human():
    result = AgentResult(reply_text="Erro técnico", intent="commerce", safety_reason="image_identify_low_confidence")
    result = enrich_handoff_metadata(IncomingMessage(channel="instagram", text="Qual valor?"), result, recent_turns=[])
    assert "modelo dessa foto" in result.reply_text
    assert "outra foto" in result.reply_text
    assert result.response_metadata["failure_explanation"]
    assert result.response_metadata["handoff"]["offer"]
    assert not result.handoff_required


@pytest.mark.parametrize("ids,expected", [([], False), (["conversation-a"], True)])
def test_confirmation_requires_queue_success(monkeypatch, ids, expected):
    import app.ops.handoff_queue as queue
    monkeypatch.setattr(queue, "mark_conversa_for_human_handoff", lambda *a, **k: ids)
    result = build_human_handoff_result(reason="customer_requested_human")
    incoming = IncomingMessage(channel="instagram", text="Quero um atendente")
    rebuilt = result_from_outbox_row({"reply_payload": build_outbound_envelope(incoming, result)})
    assert ensure_handoff_queued(incoming, rebuilt) is expected


def test_farewell_does_not_ask_for_name_and_greeting_is_not_name():
    from app.sales.qualification_slots import classify_qualification_question, _is_plausible_name
    assert classify_qualification_question("Se precisar, é só chamar por aqui.") is None
    assert not _is_plausible_name("bom dia")


@pytest.mark.asyncio
async def test_instagram_never_synthesizes_audio(monkeypatch):
    import app.message_pipeline as pipeline
    monkeypatch.setattr(pipeline, "get_settings", lambda: pytest.fail("audio settings must not be read"))
    result = AgentResult(reply_text="Assistente virtual (IA)\n\nOlá!", intent="commerce")
    assert await pipeline.enrich_agent_result(IncomingMessage(channel="instagram", input_modality="audio"), result) is result


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["instagram", "whatsapp"])
async def test_provider_does_not_send_unqueued_confirmation(monkeypatch, channel):
    from types import SimpleNamespace
    import app.ops.handoff_queue as queue
    from app.channels import meta_instagram, brevo_client
    monkeypatch.setattr(queue, "mark_conversa_for_human_handoff", lambda *a, **k: [])
    result = build_human_handoff_result(reason="customer_requested_human")
    incoming = IncomingMessage(channel=channel, sender_external_id="123", text="Quero um atendente")
    if channel == "instagram":
        monkeypatch.setattr(meta_instagram, "get_settings", lambda: SimpleNamespace(meta_page_access_token="test-only", meta_ig_business_account_id="456"))
        sent = await meta_instagram.send_meta_instagram_reply(incoming, result)
        assert sent == {"ok": False, "error": "human_handoff_queue_unavailable"}
    else:
        monkeypatch.setattr(brevo_client, "get_settings", lambda: SimpleNamespace(dry_run=False, brevo_reply_mode="conversations"))
        sent = await brevo_client.send_brevo_reply(incoming, result)
        assert not sent.ok and sent.error == "human_handoff_queue_unavailable"
