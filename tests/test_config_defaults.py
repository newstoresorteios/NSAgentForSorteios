from app.config import Settings, get_settings


def test_openai_model_fallback_is_gpt_4_1_mini():
    assert Settings.model_fields["openai_model"].default == "gpt-4.1-mini"


def test_pix_direct_defaults_are_safe_off():
    assert Settings.model_fields["pix_direct_enabled"].default is False
    assert Settings.model_fields["pix_exp_min"].default == 30
    assert (
        Settings.model_fields["mp_base_url"].default == "https://api.mercadopago.com"
    )


def test_history_window_defaults_separate_model_and_recovery():
    assert Settings.model_fields["agent_history_limit"].default == 12
    assert Settings.model_fields["agent_history_hard_cap"].default == 80
    assert Settings.model_fields["agent_max_recent_turns"].default == 8


def test_legacy_history_limit_200_does_not_crash_and_coerces_to_model_window(monkeypatch):
    """Production Vercel still had AGENT_HISTORY_LIMIT=200 from the old setup."""
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_HISTORY_LIMIT", "200")
    monkeypatch.setenv("AGENT_HISTORY_HARD_CAP", "80")
    try:
        settings = Settings()
    finally:
        get_settings.cache_clear()
    assert settings.agent_history_limit == 12
    assert settings.agent_history_hard_cap == 80


def test_llm_budget_defaults_enforce_three_calls():
    assert Settings.model_fields["agent_llm_budget_enabled"].default is True
    assert Settings.model_fields["agent_max_llm_calls_per_turn"].default == 3
    assert Settings.model_fields["agent_critique_mode"].default == "off"
    assert Settings.model_fields["agent_critique_max_retries"].default == 1
    assert Settings.model_fields["agent_quality_judge_mode"].default == "shadow"
    assert Settings.model_fields["agent_quality_judge_risk_threshold"].default == 70


def test_persona_and_memory_rollout_defaults():
    assert Settings.model_fields["agent_db_persona_enabled"].default is True
    assert Settings.model_fields["agent_memory_proposals_enabled"].default is True
    assert Settings.model_fields["agent_memory_auto_apply_enabled"].default is False
    assert Settings.model_fields["agent_contact_memory_in_prompt_enabled"].default is True
    assert Settings.model_fields["agent_conversation_summary_enabled"].default is False
