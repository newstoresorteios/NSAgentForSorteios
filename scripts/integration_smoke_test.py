"""Smoke test de integração: NSAgent + TRAYadaptor + Chatbo (+ Supabase opcional).

Uso: python scripts/integration_smoke_test.py
Nunca imprime segredos — tokens são mascarados na saída.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_S = 10.0
PLACEHOLDER_NSAGENT = "https://ns-agent-for-sorteios.vercel.app"


@dataclass(frozen=True)
class SmokeConfig:
    nsagent_base_url: str
    tray_adapter_url: str
    tray_adapter_token: str
    chatbo_base_url: str
    supabase_url: str
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class CheckResult:
    name: str
    label: str
    critical: bool
    passed: bool
    detail: str


def mask_secret(value: str, visible: int = 4) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "(vazio)"
    if len(cleaned) <= visible * 2:
        return "***"
    return f"{cleaned[:visible]}…{cleaned[-visible:]}"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        if cleaned.upper() in {"[SENSITIVE]", "SENSITIVE"}:
            continue
        if key and key not in os.environ:
            os.environ[key] = cleaned


def load_local_env() -> None:
    for name in (".env.local", ".env", ".env.vercel.cron"):
        _load_dotenv(ROOT / name)


def normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _http_check(
    client: httpx.Client,
    cfg: SmokeConfig,
    *,
    name: str,
    label: str,
    critical: bool,
    base: str,
    path: str,
    missing_msg: str,
    evaluate: Callable[[httpx.Response, dict[str, Any]], tuple[bool, str]],
) -> CheckResult:
    if not base:
        return CheckResult(name, label, critical, False, missing_msg)
    try:
        response = client.get(f"{base}{path}", timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, critical, False, f"erro HTTP: {type(exc).__name__}")
    ok, detail = evaluate(response, _json_body(response))
    return CheckResult(name, label, critical, ok, detail)


def check_nsagent_health(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    def evaluate(response: httpx.Response, body: dict[str, Any]) -> tuple[bool, str]:
        version = body.get("agent_version")
        probe = body.get("tray_adaptor_probe") or {}
        probe_ok = bool(probe.get("ok"))
        ok = response.status_code == 200 and body.get("ok") is True and bool(version) and probe_ok
        detail = f"HTTP {response.status_code}; agent_version={version!r}; tray_adaptor_probe.ok={probe_ok}"
        if response.status_code == 200 and not probe_ok:
            detail += f"; motivo={probe.get('reason') or probe.get('error') or 'probe falhou'}"
        return ok, detail

    return _http_check(
        client, cfg, name="nsagent_health", label="NSAgent /api/health", critical=True,
        base=normalize_base(cfg.nsagent_base_url), path="/api/health",
        missing_msg="NSAGENT_BASE_URL não configurada", evaluate=evaluate,
    )


def check_tray_health_tray(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    def evaluate(response: httpx.Response, body: dict[str, Any]) -> tuple[bool, str]:
        ok = response.status_code == 200 and body.get("access_valid") is True
        detail = (
            f"HTTP {response.status_code}; access_valid={body.get('access_valid')!r}; "
            f"store_id={body.get('store_id')!r}"
        )
        return ok, detail

    return _http_check(
        client, cfg, name="tray_health_tray", label="TRAYadaptor /health/tray", critical=True,
        base=normalize_base(cfg.tray_adapter_url), path="/health/tray",
        missing_msg="TRAY_ADAPTER_URL não configurada", evaluate=evaluate,
    )


def check_tray_health_basic(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    def evaluate(response: httpx.Response, body: dict[str, Any]) -> tuple[bool, str]:
        ok = response.status_code == 200 and body.get("status") == "ok"
        return ok, f"HTTP {response.status_code}; status={body.get('status')!r}"

    return _http_check(
        client, cfg, name="tray_health_basic", label="TRAYadaptor /health", critical=False,
        base=normalize_base(cfg.tray_adapter_url), path="/health",
        missing_msg="TRAY_ADAPTER_URL não configurada", evaluate=evaluate,
    )


def check_chatbo_health(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    def evaluate(response: httpx.Response, body: dict[str, Any]) -> tuple[bool, str]:
        status = body.get("status")
        ok = response.status_code == 200 and status == "ok"
        detail = f"HTTP {response.status_code}; status={status!r}"
        if status == "degraded":
            detail += f"; missing_env={body.get('missing_env') or []}"
        return ok, detail

    return _http_check(
        client, cfg, name="chatbo_health", label="Chatbo /health", critical=True,
        base=normalize_base(cfg.chatbo_base_url), path="/health",
        missing_msg="CHATBO_BASE_URL não configurada", evaluate=evaluate,
    )


def check_env_alignment(cfg: SmokeConfig) -> CheckResult:
    tray_url = normalize_base(cfg.tray_adapter_url)
    token = (cfg.tray_adapter_token or "").strip()
    parts = [
        f"TRAY_ADAPTER_URL={tray_url}" if tray_url else "TRAY_ADAPTER_URL ausente",
        f"TRAY_ADAPTER_TOKEN={mask_secret(token)}" if token else "TRAY_ADAPTER_TOKEN ausente",
    ]
    return CheckResult("env_alignment", "Variáveis TRAY (local)", False, bool(tray_url and token), "; ".join(parts))


def check_supabase_ping(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    def evaluate(response: httpx.Response, _body: dict[str, Any]) -> tuple[bool, str]:
        ok = response.status_code in {200, 400, 401, 404}
        return ok, f"HTTP {response.status_code} em /rest/v1/"

    base = normalize_base(cfg.supabase_url)
    if not base:
        return CheckResult("supabase_ping", "Supabase REST (opcional)", False, True, "ignorado (SUPABASE_URL vazio)")
    return _http_check(
        client, cfg, name="supabase_ping", label="Supabase REST (opcional)", critical=False,
        base=base, path="/rest/v1/", missing_msg="ignorado", evaluate=evaluate,
    )


def run_smoke_tests(cfg: SmokeConfig, *, client: httpx.Client | None = None) -> list[CheckResult]:
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True)
    try:
        return [
            check_nsagent_health(http, cfg),
            check_tray_health_tray(http, cfg),
            check_tray_health_basic(http, cfg),
            check_chatbo_health(http, cfg),
            check_env_alignment(cfg),
            check_supabase_ping(http, cfg),
        ]
    finally:
        if owns_client:
            http.close()


def parse_config_from_env() -> SmokeConfig:
    try:
        timeout_s = float(os.getenv("SMOKE_TEST_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    return SmokeConfig(
        nsagent_base_url=os.getenv("NSAGENT_BASE_URL", PLACEHOLDER_NSAGENT),
        tray_adapter_url=os.getenv("TRAY_ADAPTER_URL", ""),
        tray_adapter_token=os.getenv("TRAY_ADAPTER_TOKEN", ""),
        chatbo_base_url=os.getenv("CHATBO_BASE_URL", ""),
        supabase_url=os.getenv("SUPABASE_URL", ""),
        timeout_s=timeout_s,
    )


def print_report(results: list[CheckResult]) -> int:
    print("=== Smoke test de integração (NSAgent + TRAY + Chatbo) ===\n")
    header = f"{'Check':<28} {'Crítico':<8} {'Resultado':<8} Detalhe"
    print(header)
    print("-" * len(header))
    for row in results:
        print(f"{row.label:<28} {'sim' if row.critical else 'não':<8} {'PASS' if row.passed else 'FAIL':<8} {row.detail}")
    critical_failed = [r for r in results if r.critical and not r.passed]
    optional_failed = [r for r in results if not r.critical and not r.passed]
    print()
    if critical_failed:
        print(f"FALHA: {len(critical_failed)} check(s) crítico(s) com problema.")
        return 1
    if optional_failed:
        print(f"OK (críticos): {len(optional_failed)} check(s) opcional(is) falharam — revise TRAY env ou Supabase.")
    else:
        print("OK: todos os checks passaram.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test HTTP dos serviços integrados.")
    parser.add_argument("--no-dotenv", action="store_true", help="Não carregar .env automaticamente.")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_local_env()
    return print_report(run_smoke_tests(parse_config_from_env()))


if __name__ == "__main__":
    sys.exit(main())
