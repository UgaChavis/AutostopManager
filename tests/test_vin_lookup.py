from __future__ import annotations

from autostop_manager.vin_lookup import build_lookup_plan, classify_identifier


def test_classify_identifier_detects_vin_and_frame_number():
    vin = classify_identifier("1HGCM82633A004352")
    assert vin.kind == "vin"
    assert vin.normalized == "1HGCM82633A004352"

    frame = classify_identifier("GXE10-0088644")
    assert frame.kind == "frame_number"
    assert frame.normalized == "GXE10-0088644"
    assert frame.market_hint == "japan"


def test_build_lookup_plan_uses_make_specific_sources(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://example.test",
            "vin": vin,
            "vehicle": {
                "make": "Toyota",
                "model": "Camry",
                "modelyear": 2020,
            },
        }

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fake_decode_vin_vpic)

    plan = build_lookup_plan("4T1BF1FK0LU000000")
    assert plan["identifier"]["kind"] == "vin"
    assert plan["decoded_vehicle"]["make"] == "Toyota"
    assert any(step["source_name"] == "Toyota Japan EPC Help" for step in plan["steps"])


def test_build_lookup_plan_for_frame_number_returns_japan_routes():
    plan = build_lookup_plan("GXE10-0088644")
    assert plan["identifier"]["kind"] == "frame_number"
    assert plan["steps"]
    assert any(step["source_name"] == "Toyota Japan EPC Help" for step in plan["steps"])
    assert any("brand" in warning.lower() for warning in plan["warnings"])


def test_build_lookup_plan_with_make_hint_narrows_frame_routes():
    plan = build_lookup_plan("V10-030867", make_hint="Nissan")
    assert plan["identifier"]["kind"] == "frame_number"
    assert plan["steps"]
    assert plan["steps"][0]["source_name"] == "Nissan EPC Mirror"
