import httpx

from scripts.probe_release_gates import sample_health


def test_probe_counts_http_and_contract_failures_without_exposing_body(monkeypatch):
    def get(url, **kwargs):
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json={"ok": False, "secret": "must-not-appear"})
    monkeypatch.setattr("scripts.probe_release_gates.httpx.get", get)
    report = sample_health("https://agent.test", "/api/health", "agent")
    assert report["requests"] == report["errors"] == 6
    assert "must-not-appear" not in str(report)


def test_probe_counts_timeouts(monkeypatch):
    def get(url, **kwargs):
        raise httpx.ReadTimeout("private upstream detail")
    monkeypatch.setattr("scripts.probe_release_gates.httpx.get", get)
    report = sample_health("https://agent.test", "/api/health", "agent")
    assert report["errors"] == 6
    assert "private" not in str(report)
