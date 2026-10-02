import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import cron_release_preflight as gate

SHA = "a1b2c3d4e5f6" + "7" * 28


@pytest.mark.parametrize("base", ["", "https://old-deployment.vercel.app", gate.PRODUCTION_ALIAS + "/old",
                                   gate.PRODUCTION_ALIAS + "?key=secret", "http://ns-agent-for-sorteios.vercel.app",
                                   "https://secret@ns-agent-for-sorteios.vercel.app", gate.PRODUCTION_ALIAS + ".evil"])
def test_unknown_destination_fails_without_http_request(base):
    with pytest.raises(gate.PreflightError, match="production_alias_required"):
        gate.check_release(base, SHA, fetcher=lambda: pytest.fail("unapproved destination must not be queried"))


@pytest.mark.parametrize("reported", [None, "unknown", "oldrelease12", SHA, "b" * 12])
def test_old_unknown_or_wrong_length_deployment_fails(reported):
    with pytest.raises(gate.PreflightError, match="deployment_sha_mismatch"):
        gate.check_release(gate.PRODUCTION_ALIAS, SHA, fetcher=lambda: {"ok": True, "deployment_sha": reported})


def test_published_short_sha_must_match_the_workflow_revision():
    assert gate.check_release(gate.PRODUCTION_ALIAS + "/", SHA,
        fetcher=lambda: {"ok": True, "deployment_sha": SHA[:12]}) == SHA[:12]
    with pytest.raises(gate.PreflightError, match="workflow_sha_missing_or_invalid"):
        gate.check_release(gate.PRODUCTION_ALIAS, "", fetcher=lambda: pytest.fail("missing revision"))


def test_health_transport_is_get_only_unauthenticated_bounded_and_never_redirects(monkeypatch):
    observed = {}

    class Response(io.BytesIO):
        status = 200

        def read(self, size=-1):
            observed["read_size"] = size
            return super().read(size)

    class Opener:
        def open(self, request, timeout):
            observed.update(url=request.full_url, method=request.get_method(), headers=dict(request.header_items()), timeout=timeout)
            return Response(json.dumps({"ok": True, "deployment_sha": SHA[:12]}).encode())

    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], gate.NoRedirect)
        assert handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://other") is None
        return Opener()

    monkeypatch.setattr(gate, "build_opener", build)
    assert gate.fetch_health()["deployment_sha"] == SHA[:12]
    assert observed["url"] == gate.HEALTH_URL and observed["method"] == "GET"
    assert not any(key.lower() == "authorization" for key in observed["headers"])
    assert observed["timeout"] == 10 and observed["read_size"] == gate.MAX_HEALTH_BYTES + 1


def test_http_failure_outputs_only_safe_error_and_nonzero_exit(monkeypatch, capsys):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("https://secret:token@invalid", 302, "sensitive-response", {}, None)

    monkeypatch.setattr(gate, "build_opener", lambda *_: Opener())
    assert gate.main({"AGENT_BASE_URL": gate.PRODUCTION_ALIAS, "GITHUB_SHA": SHA}) == 1
    output = capsys.readouterr()
    assert output.err.strip() == "cron_preflight_failed: health_unavailable_or_invalid"
    assert not output.out


@pytest.mark.parametrize("name", ["attendance-learning.yml", "remarketing.yml"])
def test_each_writer_checks_the_release_before_the_only_post(name):
    workflow = (Path(__file__).parents[2] / ".github/workflows" / name).read_text(encoding="utf-8")
    assert workflow.index("actions/checkout@v4") < workflow.index("python3 scripts/cron_release_preflight.py")
    assert workflow.index("python3 scripts/cron_release_preflight.py") < workflow.index("-X POST")
    assert workflow.count("-X POST") == 1
    assert 'BASE="https://ns-agent-for-sorteios.vercel.app"' in workflow
    assert "continue-on-error" not in workflow and "if: always()" not in workflow
    if name == "remarketing.yml":
        assert "schedule:" not in workflow
        assert "if: false" in workflow
