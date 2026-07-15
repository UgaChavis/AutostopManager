from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_control_center_dashboard_static_assets_exist():
    base = ROOT / "frontend" / "control-center"
    index = (base / "index.html").read_text(encoding="utf-8")
    app = (base / "app.js").read_text(encoding="utf-8")
    styles = (base / "styles.css").read_text(encoding="utf-8")

    assert "Control Center" in index
    assert "Environment" in index
    assert "Runtime & Production" in index
    assert "./control-report.json" in app
    assert "ControlReportV1" in app
    assert "renderEnvironment" in app
    assert "renderRuntimeProduction" in app
    assert "function asArray" in app
    assert "function metricRow" in app
    assert "escapeHtml(value)" in app
    assert "Array.isArray(rawReport)" in app
    assert "summary-grid" in styles
    assert "status-green" in styles
