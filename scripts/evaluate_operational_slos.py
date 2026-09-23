"""Evaluate exported turn.quality JSONL locally. No network or model calls.

Exit: 0 = measured SLOs passed, 1 = breach, 2 = insufficient telemetry.
Input must be restricted to one deployment/workspace and a declared time window.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def evaluate(events: list[dict], *, min_samples: int = 20) -> dict:
    samples = [e for e in events if e.get("event") == "turn.quality"]
    alerts = []
    missing = []
    metrics = {}
    # Critical events must fire even for one sample, including cold instances.
    for field in ("tenant_isolation_breach", "duplicate_order", "false_payment_confirmation"):
        count = sum(e.get(field) is True for e in samples)
        metrics[field] = {"observed": sum(isinstance(e.get(field), bool) for e in samples), "incidents": count}
        if count:
            alerts.append({"code": field, "severity": "critical", "incidents": count})
        if not samples or any(not isinstance(e.get(field), bool) for e in samples):
            missing.append(field)
    specs = (
        ("fallback_reason", "fallback_rate", 0.10),
        ("factual_valid", "factual_invalid_rate", 0.02),
    )
    for field, code, threshold in specs:
        known = [e for e in samples if field in e and (
            isinstance(e[field], bool) if field == "factual_valid"
            else e[field] is None or isinstance(e[field], str)
        )]
        bad = sum(e[field] is False if field == "factual_valid" else bool(e[field]) for e in known)
        rate = bad / len(known) if known else None
        metrics[code] = {"samples": len(known), "value": rate, "maximum": threshold}
        if len(known) < min_samples or len(known) != len(samples):
            missing.append(code)
        if len(known) >= min_samples and rate > threshold:
            alerts.append({"code": code, "severity": "warning", "value": rate})
    latencies = sorted(float(e["latency_ms"]) for e in samples
                       if isinstance(e.get("latency_ms"), (int, float))
                       and not isinstance(e["latency_ms"], bool)
                       and math.isfinite(e["latency_ms"]) and e["latency_ms"] >= 0)
    p95 = latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None
    metrics["latency_p95_ms"] = {"samples": len(latencies), "value": p95, "maximum": 20000}
    if len(latencies) < min_samples or len(latencies) != len(samples):
        missing.append("latency_p95_ms")
    if len(latencies) >= min_samples and p95 > 20000:
        alerts.append({"code": "latency_p95_ms", "severity": "warning", "value": p95})
    return {"status": "failed" if alerts else "inconclusive" if missing else "passed",
            "samples": len(samples), "metrics": metrics, "alerts": alerts, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        events = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not all(isinstance(e, dict) for e in events):
            raise ValueError("expected objects")
        report = evaluate(events)
    except (OSError, ValueError):
        print(json.dumps({"status": "inconclusive", "error": "invalid_input"}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return {"passed": 0, "failed": 1, "inconclusive": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
