"""Testes do script de smoke test (HTTP mockado — sem URLs live)."""

from __future__ import annotations

import httpx

from scripts.integration_smoke_test import (
    CheckResult,
    SmokeConfig,
    check_chatbo_health,
    check_env_alignment,
    check_nsagent_health,
    check_tray_health_tray,
    print_report,
    run_smoke_tests,
)


def _cfg(**overrides) -> SmokeConfig:
    base = {
        "nsagent_base_url": "https://nsagent.test",
        "tray_adapter_url": "https://tray.test",
        "tray_adapter_token": "secret-token-value",
        "chatbo_base_url": "https://chatbo.test",
        "supabase_url": "",
        "timeout_s": 5.0,
    }
    base.update(overrides)
    return SmokeConfig(**base)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_nsagent_health_pass():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/health"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "agent_version": "v61",
                "tray_adaptor_probe": {"ok": True, "configured": True},
            },
        )

    with _mock_client(handler) as client:
        result = check_nsagent_health(client, _cfg())
    assert result.passed is True
    assert "v61" in result.detail


def test_tray_health_tray_requires_access_valid():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/tray"
        return httpx.Response(200, json={"access_valid": False, "store_id": "1"})

    with _mock_client(handler) as client:
        result = check_tray_health_tray(client, _cfg())
    assert result.passed is False


def test_chatbo_health_degraded_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "degraded", "missing_env": ["JWT_SECRET"]})

    with _mock_client(handler) as client:
        result = check_chatbo_health(client, _cfg())
    assert result.passed is False
    assert "JWT_SECRET" in result.detail


def test_env_alignment_masks_token():
    result = check_env_alignment(_cfg(tray_adapter_token="abcdefghijklmnop"))
    assert result.passed is True
    assert "abcdefghijklmnop" not in result.detail
    assert "abcd" in result.detail


def test_run_smoke_tests_all_critical_pass():
    routes = {
        "/api/health": {
            "ok": True,
            "agent_version": "v61",
            "tray_adaptor_probe": {"ok": True},
        },
        "/health/tray": {"access_valid": True, "store_id": "42"},
        "/health": {"status": "ok"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404)

    with _mock_client(handler) as client:
        results = run_smoke_tests(_cfg(), client=client)

    critical = [r for r in results if r.critical]
    assert critical
    assert all(r.passed for r in critical)


def test_print_report_exit_code_on_critical_failure(capsys):
    results = [
        CheckResult("a", "NSAgent", True, False, "falhou"),
        CheckResult("b", "Opcional", False, True, "ok"),
    ]
    code = print_report(results)
    captured = capsys.readouterr().out
    assert code == 1
    assert "FALHA" in captured


def test_print_report_exit_zero_when_only_optional_fails(capsys):
    results = [
        CheckResult("a", "NSAgent", True, True, "ok"),
        CheckResult("b", "Supabase", False, False, "timeout"),
    ]
    code = print_report(results)
    assert code == 0
    assert "OK (críticos)" in capsys.readouterr().out
