from app.config import Settings


def test_openai_model_fallback_is_gpt_4_1_mini():
    assert Settings.model_fields["openai_model"].default == "gpt-4.1-mini"


def test_pix_direct_defaults_are_safe_off():
    assert Settings.model_fields["pix_direct_enabled"].default is False
    assert Settings.model_fields["pix_exp_min"].default == 30
    assert (
        Settings.model_fields["mp_base_url"].default == "https://api.mercadopago.com"
    )
