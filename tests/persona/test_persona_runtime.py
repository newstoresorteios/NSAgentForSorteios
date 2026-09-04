from __future__ import annotations

from app.persona.persona_models import PersonaVersion
from app.persona.persona_runtime import (
    _as_prompt_list,
    apply_policy_overrides,
    build_persona_runtime,
    extract_pix_discount_percent,
    get_persona_runtime,
    reset_persona_runtime,
    set_persona_runtime,
)


def _persona(**overrides) -> PersonaVersion:
    payload = {
        "id": 17,
        "tenant_id": "newstore",
        "persona_key": "newstore_commercial",
        "version": 17,
        "name": "Crono",
        "instructions": (
            "Eu sou o Crono. O cliente tem 15% de desconto pagando no PIX. "
            "Nunca ofereça desconto além dos 15% do PIX."
        ),
        "instructions_hash": "abc",
        "status": "active",
        "metadata": {},
    }
    payload.update(overrides)
    return PersonaVersion.model_validate(payload)


def test_extract_pix_discount_from_instructions():
    assert extract_pix_discount_percent(
        "O cliente tem 15% de desconto pagando no PIX."
    ) == 15
    assert extract_pix_discount_percent(
        "Nunca ofereça desconto além dos 15% do PIX"
    ) == 15
    assert extract_pix_discount_percent("sem desconto aqui") is None


def test_build_runtime_from_metadata_policy():
    persona = _persona(
        metadata={
            "chatboPersonaId": "11111111-1111-1111-1111-111111111111",
            "runtime_policy": {
                "pix_discount_percent": 12,
                "require_cart_for_informational_payment": True,
                "greeting_mode": "local",
                "agent_display_name": "Crono NS",
            },
        }
    )
    runtime = build_persona_runtime(active=persona)
    assert runtime.enabled is True
    assert runtime.pix_discount_percent == 12
    assert runtime.require_cart_for_informational_payment is True
    assert runtime.greeting_mode == "local"
    assert runtime.agent_display_name == "Crono NS"
    assert runtime.policy_source == "metadata"
    assert runtime.active_persona is persona


def test_build_runtime_parses_instructions_when_no_metadata_policy():
    runtime = build_persona_runtime(active=_persona())
    assert runtime.pix_discount_percent == 15
    assert runtime.max_pix_discount_percent == 15
    assert runtime.require_cart_for_informational_payment is False
    assert runtime.policy_source == "instructions_parse"


def test_build_runtime_reads_chatbo_greeting_and_tone():
    persona = _persona(
        metadata={"chatboPersonaId": "11111111-1111-1111-1111-111111111111"}
    )
    runtime = build_persona_runtime(
        active=persona,
        chatbo_profile={
            "name": "Crono New Store",
            "tone": "consultative",
            "greeting": "Olá! Eu sou o Crono, assistente virtual da New Store Relógios.",
            "closing_message": "Obrigado por falar com a New Store!",
            "customer_address_style": "Sempre pelo primeiro nome.",
            "recommendation_rules": [
                "Nunca apresentar mais de 3 peças de uma vez",
                "priorizar pronta entrega",
            ],
        },
    )
    assert runtime.agent_display_name == "Crono"
    assert runtime.tone == "Consultivo"
    assert "Eu sou o Crono" in (runtime.greeting_text or "")
    assert runtime.closing_message
    assert "primeiro nome" in (runtime.customer_address_style or "")
    assert runtime.max_catalog_options == 3
    assert runtime.prefer_ready_stock is True
    assert "Limite operacional" in runtime.sales_skills_block()


def test_contextvar_roundtrip():
    runtime = build_persona_runtime(active=_persona())
    token = set_persona_runtime(runtime)
    try:
        assert get_persona_runtime() is runtime
        assert "pix_discount_percent: 15" in get_persona_runtime().interpreter_policy_block()
    finally:
        reset_persona_runtime(token)
    assert get_persona_runtime() is None


def test_apply_policy_overrides_clamps_discount():
    base = build_persona_runtime(active=_persona())
    updated = apply_policy_overrides(
        base,
        {"pix_discount_percent": 99, "max_pix_discount_percent": 99},
        source="metadata",
    )
    assert updated.pix_discount_percent == 40
    assert updated.max_pix_discount_percent == 40


def test_as_prompt_list_logs_invalid_json(capsys):
    assert _as_prompt_list("{not-json") == ["{not-json"]
    output = capsys.readouterr().out
    assert "[persona.runtime.prompt_list]" in output
    assert "JSONDecodeError" in output
