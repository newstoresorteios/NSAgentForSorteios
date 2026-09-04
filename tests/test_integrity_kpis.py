"""Integrity KPI aggregation unit tests."""

from app.ops.integrity_kpis import _classify, _pct, build_integrity_kpi_report


def test_classify_families():
    assert _classify(None) == "ok"
    assert _classify("not_found") == "not_found"
    assert _classify("exact_product_ambiguous_brand") == "ambiguous"
    assert _classify("factual_validation_failed") == "factual_fail"
    assert _classify("tray_adapter_unavailable") == "tray_down"
    assert _classify("compliance_preference_reresearch") == "compliance_applied"


def test_pct_rounding():
    assert _pct(21, 100) == 21.0
    assert _pct(0, 0) == 0.0


def test_build_report_without_database(monkeypatch):
    from app.ops import integrity_kpis as mod

    class _Cfg:
        database_url = ""
        agent_async_ingress_enabled = False

    monkeypatch.setattr(mod, "get_settings", lambda: _Cfg())
    report = build_integrity_kpi_report(days=7)
    assert report["total_responses"] == 0
    assert report["rates"]["not_found_pct"] == 0.0
    assert report["queues"]["configured"] is False
