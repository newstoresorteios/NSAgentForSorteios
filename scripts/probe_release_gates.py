"""Bounded read-only live probes. No OpenAI calls, checkout, or customer messages.

Use explicit environment configuration (same names as integration_smoke_test).
Reports contain only status, counts and timings; never response bodies or tokens.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

import httpx

try:
    from scripts.integration_smoke_test import load_local_env, parse_config_from_env, run_smoke_tests
except ModuleNotFoundError:
    from integration_smoke_test import load_local_env, parse_config_from_env, run_smoke_tests


def sample_health(base: str, path: str, expected: str, *, count: int = 6) -> dict:
    def one(_):
        started = time.perf_counter()
        try:
            response = httpx.get(base.rstrip("/") + path, timeout=15, follow_redirects=False)
            body = response.json()
            valid = isinstance(body, dict) and (
                body.get("ok") is True if expected == "agent" else body.get("status") == "ok"
            )
            return response.status_code == 200 and valid, (time.perf_counter() - started) * 1000
        except (httpx.HTTPError, ValueError):
            return False, (time.perf_counter() - started) * 1000
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(one, range(count)))
    times = sorted(row[1] for row in rows)
    return {"requests": count, "concurrency": 2, "errors": sum(not r[0] for r in rows),
            "p95_ms": round(times[math.ceil(count * .95) - 1], 2),
            "scope": "health_only_not_commercial_capacity"}


def run() -> dict:
    cfg = parse_config_from_env()
    checks = run_smoke_tests(cfg)
    samples = {}
    for name, base, path, expected in (
        ("nsagent", cfg.nsagent_base_url, "/api/health", "agent"),
        ("tray", cfg.tray_adapter_url, "/health", "service"),
        ("chatbo", cfg.chatbo_base_url, "/health", "service"),
    ):
        if base:
            samples[name] = sample_health(base, path, expected)
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "openai_calls": 0,
            "checks": [asdict(c) for c in checks], "health_samples": samples,
            "smoke_passed": all(c.passed for c in checks if c.critical)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_local_env()
    report = run()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["smoke_passed"] and all(v["errors"] == 0 for v in report["health_samples"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
