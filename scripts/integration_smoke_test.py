"""Smoke test de integração: NSAgent + TRAYadaptor + Chatbo (+ Supabase opcional).

Uso (na raiz do repo, com variáveis de ambiente):
  python scripts/integration_smoke_test.py

Nunca imprime segredos — tokens são mascarados na saída.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    _load_dotenv(ROOT / ".env.local")
    _load_dotenv(ROOT / ".env")
    _load_dotenv(ROOT / ".env.vercel.cron")


def normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def check_nsagent_health(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    name = "nsagent_health"
    label = "NSAgent /api/health"
    base = normalize_base(cfg.nsagent_base_url)
    if not base:
        return CheckResult(name, label, True, False, "NSAGENT_BASE_URL não configurada")
    url = f"{base}/api/health"
    try:
        response = client.get(url, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, True, False, f"erro HTTP: {type(exc).__name__}")
    body = _json_body(response)
    version = body.get("agent_version")
    probe = body.get("tray_adaptor_probe") or {}
    probe_ok = bool(probe.get("ok"))
    ok = response.status_code == 200 and body.get("ok") is True and bool(version) and probe_ok
    detail = (
        f"HTTP {response.status_code}; agent_version={version!r}; "
        f"tray_adaptor_probe.ok={probe_ok}"
    )
    if response.status_code == 200 and not probe_ok:
        reason = probe.get("reason") or probe.get("error") or "probe falhou"
        detail += f"; motivo={reason}"
    return CheckResult(name, label, True, ok, detail)


def check_tray_health_tray(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    name = "tray_health_tray"
    label = "TRAYadaptor /health/tray"
    base = normalize_base(cfg.tray_adapter_url)
    if not base:
        return CheckResult(name, label, True, False, "TRAY_ADAPTER_URL não configurada")
    url = f"{base}/health/tray"
    try:
        response = client.get(url, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, True, False, f"erro HTTP: {type(exc).__name__}")
    body = _json_body(response)
    access_valid = body.get("access_valid") is True
    ok = response.status_code == 200 and access_valid
    detail = (
        f"HTTP {response.status_code}; access_valid={body.get('access_valid')!r}; "
        f"store_id={body.get('store_id')!r}"
    )
    return CheckResult(name, label, True, ok, detail)


def check_tray_health_basic(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    name = "tray_health_basic"
    label = "TRAYadaptor /health"
    base = normalize_base(cfg.tray_adapter_url)
    if not base:
        return CheckResult(name, label, False, False, "TRAY_ADAPTER_URL não configurada")
    url = f"{base}/health"
    try:
        response = client.get(url, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, False, False, f"erro HTTP: {type(exc).__name__}")
    body = _json_body(response)
    ok = response.status_code == 200 and body.get("status") == "ok"
    detail = f"HTTP {response.status_code}; status={body.get('status')!r}"
    return CheckResult(name, label, False, ok, detail)


def check_chatbo_health(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    name = "chatbo_health"
    label = "Chatbo /health"
    base = normalize_base(cfg.chatbo_base_url)
    if not base:
        return CheckResult(name, label, True, False, "CHATBO_BASE_URL não configurada")
    url = f"{base}/health"
    try:
        response = client.get(url, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, True, False, f"erro HTTP: {type(exc).__name__}")
    body = _json_body(response)
    status = body.get("status")
    ok = response.status_code == 200 and status == "ok"
    detail = f"HTTP {response.status_code}; status={status!r}"
    if status == "degraded":
        missing = body.get("missing_env") or []
        detail += f"; missing_env={missing}"
    return CheckResult(name, label, True, ok, detail)


def check_env_alignment(cfg: SmokeConfig) -> CheckResult:
    name = "env_alignment"
    label = "Variáveis TRAY (local)"
    tray_url = normalize_base(cfg.tray_adapter_url)
    token = (cfg.tray_adapter_token or "").strip()
    parts: list[str] = []
    if tray_url:
        parts.append(f"TRAY_ADAPTER_URL={tray_url}")
    else:
        parts.append("TRAY_ADAPTER_URL ausente")
    if token:
        parts.append(f"TRAY_ADAPTER_TOKEN={mask_secret(token)}")
    else:
        parts.append("TRAY_ADAPTER_TOKEN ausente")
    ok = bool(tray_url and token)
    return CheckResult(name, label, False, ok, "; ".join(parts))


def check_supabase_ping(client: httpx.Client, cfg: SmokeConfig) -> CheckResult:
    name = "supabase_ping"
    label = "Supabase REST (opcional)"
    base = normalize_base(cfg.supabase_url)
    if not base:
        return CheckResult(name, label, False, True, "ignorado (SUPABASE_URL vazio)")
    url = f"{base}/rest/v1/"
    try:
        response = client.get(url, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        return CheckResult(name, label, False, False, f"erro HTTP: {type(exc).__name__}")
    # 401/400 ainda indicam reachability sem enviar chaves.
    ok = response.status_code in {200, 400, 401, 404}
    detail = f"HTTP {response.status_code} em /rest/v1/"
    return CheckResult(name, label, False, ok, detail)


def run_smoke_tests(
    cfg: SmokeConfig,
    *,
    client: httpx.Client | None = None,
) -> list[CheckResult]:
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
    timeout_raw = os.getenv("SMOKE_TEST_TIMEOUT_S", str(DEFAULT_TIMEOUT_S))
    try:
        timeout_s = float(timeout_raw)
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
        crit = "sim" if row.critical else "não"
        status = "PASS" if row.passed else "FAIL"
        print(f"{row.label:<28} {crit:<8} {status:<8} {row.detail}")
    critical_failed = [r for r in results if r.critical and not r.passed]
    optional_failed = [r for r in results if not r.critical and not r.passed]
    print()
    if critical_failed:
        print(f"FALHA: {len(critical_failed)} check(s) crítico(s) com problema.")
        return 1
    if optional_failed:
        print(
            f"OK (críticos): {len(optional_failed)} check(s) opcional(is) falharam — "
            "revise TRAY env ou Supabase se necessário."
        )
    else:
        print("OK: todos os checks passaram.")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke test HTTP dos serviços integrados (sem expor segredos).",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Não carregar .env / .env.local automaticamente.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.no_dotenv:
        load_local_env()
    cfg = parse_config_from_env()
    results = run_smoke_tests(cfg)
    return print_report(results)


if __name__ == "__main__":
    sys.exit(main())
