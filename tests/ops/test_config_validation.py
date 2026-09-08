from types import SimpleNamespace

from app.ops.config_validation import configuration_warnings


def _settings(**overrides):
    values = {
        "agent_async_ingress_enabled": False,
        "database_url": "postgresql://configured",
        "agent_memory_auto_apply_enabled": False,
        "agent_memory_auto_apply_sender_allowlist": "",
        "tray_adapter_url": "https://tray.example",
        "tray_adapter_token": "secret",
        "pix_direct_enabled": False,
        "agent_critique_enforce_on_commerce": True,
        "agent_critique_mode": "enforce",
        "environment": "production",
        "dry_run": False,
        "brevo_webhook_secret": "secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_valid_production_profile_has_no_warnings():
    assert configuration_warnings(_settings()) == []


def test_cross_setting_mismatches_are_reported_without_values():
    warnings = configuration_warnings(
        _settings(
            agent_async_ingress_enabled=True,
            database_url="",
            agent_memory_auto_apply_enabled=True,
            tray_adapter_token="",
            pix_direct_enabled=True,
            public_url="",
            mp_access_token="",
            agent_critique_mode="shadow",
            brevo_webhook_secret="",
        )
    )
    assert set(warnings) == {
        "async_ingress_requires_database",
        "memory_auto_apply_requires_sender_allowlist",
        "tray_adapter_url_and_token_must_be_configured_together",
        "pix_direct_requires_mercadopago_token",
        "pix_direct_requires_public_url",
        "commerce_critique_flag_requires_enforce_mode",
        "live_production_requires_brevo_webhook_secret",
    }
    assert "postgresql://configured" not in str(warnings)
    assert "https://tray.example" not in str(warnings)
