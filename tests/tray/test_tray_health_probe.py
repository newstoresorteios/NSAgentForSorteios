"""Tray adaptor health probe + HA checklist."""

from app.tray.tray_health_probe import tray_ha_checklist


def test_tray_ha_checklist_includes_dashboard_steps():
    checklist = tray_ha_checklist({"ok": True})
    assert checklist["adaptor_health_reachable"] is True
    assert "srv-d9fq41jtqb8s73dl4r80" in checklist["render_dashboard_url"]
    assert any("/health" in step for step in checklist["manual_steps_pending"])
    assert any("Instance Count = 2" in step for step in checklist["manual_steps_pending"])
