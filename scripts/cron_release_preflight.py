"""Read-only gate for scheduled writers; no credentials or model calls."""
from __future__ import annotations

import json
import os
import re
import sys
from urllib.request import HTTPRedirectHandler, Request, build_opener

PRODUCTION_ALIAS = "https://ns-agent-for-sorteios.vercel.app"
HEALTH_URL = PRODUCTION_ALIAS + "/api/health"
MAX_HEALTH_BYTES = 65536


class PreflightError(RuntimeError):
    """A safe error code that contains no configuration or response contents."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_health() -> dict:
    request = Request(HEALTH_URL, method="GET", headers={
        "Accept": "application/json", "Cache-Control": "no-cache",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200:
                raise PreflightError("health_http_status")
            raw = response.read(MAX_HEALTH_BYTES + 1)
        if len(raw) > MAX_HEALTH_BYTES:
            raise PreflightError("health_response_too_large")
        payload = json.loads(raw)
    except PreflightError:
        raise
    except Exception:
        # URL/HTTP errors can embed response bodies, paths and credentials.
        raise PreflightError("health_unavailable_or_invalid") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise PreflightError("health_not_ok")
    return payload


def check_release(base_url: str, github_sha: str, *, fetcher=None) -> str:
    # Exact allowlist; no deployment URLs, paths, userinfo, query or redirect.
    if (base_url or "").strip().rstrip("/") != PRODUCTION_ALIAS:
        raise PreflightError("production_alias_required")
    sha = (github_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
        raise PreflightError("workflow_sha_missing_or_invalid")
    expected = sha[:12]
    payload = (fetcher or fetch_health)()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise PreflightError("health_not_ok")
    if payload.get("deployment_sha") != expected:
        raise PreflightError("deployment_sha_mismatch")
    return expected


def main(environ=None) -> int:
    environment = os.environ if environ is None else environ
    try:
        sha = check_release(environment.get("AGENT_BASE_URL", ""), environment.get("GITHUB_SHA", ""))
    except PreflightError as exc:
        print(f"cron_preflight_failed: {exc}", file=sys.stderr)
        return 1
    print(f"cron_preflight_passed: deployment_sha={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
