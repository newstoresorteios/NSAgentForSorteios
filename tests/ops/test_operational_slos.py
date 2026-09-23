from scripts.evaluate_operational_slos import evaluate


def event(**overrides):
    return {"event": "turn.quality", "latency_ms": 1000, "fallback_reason": None,
            "factual_valid": True, "tenant_isolation_breach": False,
            "duplicate_order": False, "false_payment_confirmation": False, **overrides}


def test_empty_or_partial_telemetry_cannot_pass():
    assert evaluate([])["status"] == "inconclusive"
    assert evaluate([{"event": "turn.quality"}] * 20)["status"] == "inconclusive"
    assert evaluate([event()] * 19)["status"] == "inconclusive"


def test_one_critical_incident_fires_before_minimum_samples():
    result = evaluate([event(duplicate_order=True)])
    assert result["status"] == "failed"
    assert result["alerts"][0]["code"] == "duplicate_order"


def test_rate_latency_breaches_and_recovery():
    assert evaluate([event()] * 20)["status"] == "passed"
    report = evaluate([event()] * 16 + [event(latency_ms=21000, fallback_reason="timeout", factual_valid=False)] * 4)
    assert {a["code"] for a in report["alerts"]} == {"fallback_rate", "factual_invalid_rate", "latency_p95_ms"}


def test_unknown_critical_signal_is_not_assumed_healthy():
    sample = event()
    del sample["duplicate_order"]
    assert evaluate([sample] * 20)["status"] == "inconclusive"


def test_invalid_numeric_telemetry_is_inconclusive():
    assert evaluate([event(latency_ms=float("nan"))] * 20)["status"] == "inconclusive"
