from __future__ import annotations

from autostop_manager.vin_lookup import build_lookup_plan, classify_identifier
from autostop_manager.vin_sources import sources_for_make


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
    assert plan["catalog_routes"] == plan["steps"]
    assert "oem_candidates" in plan
    assert "supersessions" in plan
    assert "fitment_confidence" in plan
    assert "missing_context" in plan


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


def test_bmw_lookup_without_part_name_returns_route_only_dossier(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://example.test",
            "vin": vin,
            "vehicle": {"make": "BMW", "model": "X5", "modelyear": 2018},
        }

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fake_decode_vin_vpic)

    plan = build_lookup_plan("WBA00000000000000")

    assert plan["identifier"]["kind"] == "vin"
    assert plan["catalog_vehicle"]["family"] == "bmw"
    assert plan["catalog_routes"][0]["source_name"] == "partslink24 Mobile"
    assert plan["oem_candidates"] == []
    assert plan["fitment_confidence"]["level"] == "blocked"
    assert "part_name or part_group" in plan["missing_context"]


def test_vag_dsg_part_requires_epc_capture_and_gearbox_context(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://example.test",
            "vin": vin,
            "vehicle": {"make": "Volkswagen", "model": "Golf", "modelyear": 2017},
        }

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fake_decode_vin_vpic)

    plan = build_lookup_plan("WVW00000000000000", part_name="мехатроник DSG")

    route_names = [route["source_name"] for route in plan["catalog_routes"]]
    assert "Volkswagen Group ETKA" in route_names
    assert plan["catalog_vehicle"]["family"] == "vag"
    assert plan["oem_candidates"] == []
    assert plan["fitment_confidence"]["level"] == "blocked"
    assert any("mechatronic" in item for item in plan["missing_context"])
    assert any("gearbox code" in item for item in plan["missing_context"])


def test_manual_capture_builds_oem_candidate_and_supersession(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://example.test",
            "vin": vin,
            "vehicle": {"make": "BMW", "model": "X5", "modelyear": 2018},
        }

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fake_decode_vin_vpic)

    plan = build_lookup_plan(
        "WBA00000000000000",
        part_name="радиатор охлаждения",
        captured_oem_number="17 11 8 625 482",
        captured_source="BMW AIR/ETK via AOS",
        captured_supersedes="17 11 7 600 500",
    )

    assert plan["oem_candidates"][0]["normalized_number"] == "17118625482"
    assert plan["oem_candidates"][0]["source"] == "BMW AIR/ETK via AOS"
    assert plan["supersessions"][0]["from_normalized"] == "17117600500"
    assert plan["fitment_confidence"]["level"] == "high"


def test_bmw_and_vag_source_registry_has_preferred_paid_routes():
    bmw_sources = sources_for_make("BMW")
    audi_sources = sources_for_make("Audi")

    assert bmw_sources[0]["name"] == "partslink24 Mobile"
    assert audi_sources[0]["name"] == "partslink24 Mobile"
    assert any(source["name"] == "BMW AIR/ETK via AOS" for source in bmw_sources)
    assert any(source["name"] == "Volkswagen Group ETKA" for source in audi_sources)
    assert bmw_sources[0]["requires_login"] is True
    assert "oem_part_numbers" in bmw_sources[0]["outputs"]
