import pytest
from app.configuration.runtime import bind_bundle, reset_bundle, message, policy, settings_from_bundle
from app.learning.constitution import check_instruction_delta
from app.core.config import Settings, get_settings

def test_workspace_values_and_template_apply_and_reset():
    base = Settings(_env_file=None, OPENAI_API_KEY="", DATABASE_URL="")
    bundle = {"fields": [{"key": "calls", "target": "setting", "attribute": "agent_max_llm_calls_per_turn"}],
              "values": {"calls": 0, "acceptsTradeIn": False, "message.checkout": "Finalize aqui: {url}"}}
    tokens = bind_bundle(bundle, settings_from_bundle(base, bundle))
    try:
        assert get_settings().agent_max_llm_calls_per_turn == 0
        assert policy("acceptsTradeIn") is False
        assert message("checkout", url="https://example.invalid/p") == "Finalize aqui: https://example.invalid/p"
    finally:
        reset_bundle(tokens)
    assert policy("acceptsTradeIn") is True

def test_trade_in_policy_is_shared_and_human_valuation_required():
    affirmative = "A loja avalia, troca e compra relógios. Encaminhe ao consultor humano."
    denial = "A loja não avalia nem compra relógios de particulares."
    assert check_instruction_delta(affirmative, business_policy={"acceptsTradeIn": True}) == (True, None)
    assert check_instruction_delta(denial, business_policy={"acceptsTradeIn": True})[1] == "trade_in_policy_rewrite"
    assert check_instruction_delta(denial, business_policy={"acceptsTradeIn": False}) == (True, None)

def test_credentials_cannot_be_overridden_by_catalog_metadata():
    base = Settings(_env_file=None, OPENAI_API_KEY="", DATABASE_URL="")
    value = settings_from_bundle(base, {"fields": [{"key": "unsafe", "target": "setting", "attribute": "openai_api_key"}],
        "values": {"unsafe": "not-an-allowed-override"}})
    assert value.openai_api_key == ""
