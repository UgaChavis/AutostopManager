from __future__ import annotations

import json

from autostop_manager.vin_lookup import (
    build_lookup_plan,
    classify_identifier,
    decode_vin_vpic,
    decode_vins_vpic_batch,
    decode_wmi_vpic,
)
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
    assert any(step["source_name"] == "PartSouq manual catalog" for step in plan["steps"])
    amayama = next(step for step in plan["steps"] if step["source_name"] == "Amayama public catalog")
    assert amayama["source_id"] == "amayama_catalog"
    assert "amayama_catalog_manual" in amayama["aliases"]
    assert "diagram_url" in amayama["outputs"]
    assert amayama["adapter_status"] == ["manual_capture"]
    assert plan["catalog_routes"] == plan["steps"]
    assert "oem_candidates" in plan
    assert "supersessions" in plan
    assert "fitment_confidence" in plan
    assert "missing_context" in plan


def test_build_lookup_plan_redacts_raw_identifier_from_public_output(monkeypatch):
    raw_vin = "JTEBU3FJX05027767"

    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": f"https://example.test/{vin}",
            "vin": vin,
            "vehicle": {"vin": vin, "vehicledescriptor": "JTEBU3FJ*05", "make": "Toyota", "model": "Land Cruiser"},
        }

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fake_decode_vin_vpic)

    plan = build_lookup_plan(raw_vin)
    rendered = json.dumps(plan, ensure_ascii=False)

    assert raw_vin not in rendered
    assert plan["identifier"]["redacted"]["display"] == "JTE***767"
    assert plan["decoded_vehicle"]["vin"] == "JTE***767"
    assert plan["decoded_vehicle"]["vehicledescriptor"] == "JTE***767"
    assert all(raw_vin not in route["query"] for route in plan["steps"])


def test_build_lookup_plan_honors_no_live_vpic(monkeypatch):
    def fail_decode(*args, **kwargs):
        raise AssertionError("vPIC should not be called when live_vpic is false")

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fail_decode)

    plan = build_lookup_plan("JTEBU3FJX05027767", make_hint="Toyota", live_vpic=False)

    assert plan["decoded_vehicle"] == {}
    assert any("vPIC decode skipped" in warning for warning in plan["warnings"])
    assert plan["steps"]


def test_build_lookup_plan_for_frame_number_returns_japan_routes():
    plan = build_lookup_plan("GXE10-0088644")
    assert plan["identifier"]["kind"] == "frame_number"
    assert plan["steps"]
    assert any(step["source_name"] == "Toyota Japan EPC Help" for step in plan["steps"])
    assert any(step["source_name"] == "PartSouq manual catalog" for step in plan["steps"])
    assert any(step["source_name"] == "Amayama public catalog" for step in plan["steps"])
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
    assert plan["catalog_routes"][0]["source_name"] == "BMW AIR/ETK via AOS"
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
    assert "Volkswagen Group ETKA" not in route_names  # Removed unconnected Partslink24 route.
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


def test_unverified_manual_capture_cannot_raise_fitment_to_high_confidence():
    plan = build_lookup_plan(
        "WBA" + "A" * 14,
        make_hint="BMW",
        live_vpic=False,
        part_name="масляный фильтр",
        captured_oem_number="TEST12345",
        captured_source="unverified-manual-source",
    )

    assert plan["oem_candidates"][0]["confidence"] == "medium"
    assert plan["fitment_confidence"]["level"] == "medium"
    assert plan["fitment_confidence"]["score"] == 60


def test_public_web_catalog_capture_stays_preliminary_and_requests_cross_check():
    plan = build_lookup_plan(
        "NZE141-0000001",  # Synthetic frame, not a customer's vehicle.
        make_hint="Toyota",
        live_vpic=False,
        part_name="воздушный фильтр",
        captured_oem_number="TEST-OEM-001",
        captured_source="partsouq_catalog_manual",
    )

    candidate = plan["oem_candidates"][0]
    assert candidate["confidence"] == "low"
    assert candidate["source_id"] == "partsouq_catalog"
    assert candidate["source_access_mode"] == "public"
    assert any("preliminary" in action for action in plan["next_actions"])
    assert any("available cross references" in action for action in plan["next_actions"])
    assert "NZE141-0000001" not in json.dumps(plan, ensure_ascii=False)


def test_bmw_and_vag_source_registry_has_preferred_paid_routes():
    bmw_sources = sources_for_make("BMW")
    audi_sources = sources_for_make("Audi")

    assert bmw_sources[0]["name"] == "BMW AIR/ETK via AOS"
    assert all("partslink24.com" not in source.get("url", "") for source in bmw_sources + audi_sources)
    assert any(source["name"] == "BMW AIR/ETK via AOS" for source in bmw_sources)
    assert not any(source["name"] == "Volkswagen Group ETKA" for source in audi_sources)
    assert bmw_sources[0]["requires_login"] is True
    assert "oem_part_numbers" in bmw_sources[0]["outputs"]


def test_japanese_source_registry_adds_public_diagram_routes_without_duplicates():
    toyota_sources = sources_for_make("Toyota")
    names = [source["name"] for source in toyota_sources]

    assert names.count("PartSouq manual catalog") == 1
    assert names.count("Amayama public catalog") == 1
    partsouq = next(source for source in toyota_sources if source["name"] == "PartSouq manual catalog")
    assert partsouq["source_id"] == "partsouq_catalog"
    assert "partsouq_catalog_manual" in partsouq["aliases"]
    assert "applicability_conditions" in partsouq["outputs"]
    assert partsouq["access_mode"] == "public"


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_vpic_decode_request_uses_model_year_and_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=10.0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        payload = {
            "Results": [
                {
                    "VIN": "1HGCM82633A004352",
                    "Make": "Honda",
                    "Model": "Accord",
                    "ModelYear": "2003",
                }
            ]
        }
        return _FakeResponse(payload)

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fake_urlopen)

    result = decode_vin_vpic("1HGCM82633A004352", model_year=2003)

    assert result["ok"] is True
    assert result["source"] == "NHTSA vPIC"
    assert "modelyear=2003" in captured["url"]
    assert result["vehicle"]["make"] == "Honda"
    assert result["vehicle"]["modelyear"] == 2003


def test_vpic_extended_decode_uses_extended_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=10.0):
        captured["url"] = request.full_url
        payload = {
            "Results": [
                {
                    "VIN": "1HGCM82633A004352",
                    "Make": "Honda",
                    "Model": "Accord",
                    "FuelTypePrimary": "Gasoline",
                    "DisplacementL": "3.0",
                }
            ]
        }
        return _FakeResponse(payload)

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fake_urlopen)

    result = decode_vin_vpic("1HGCM82633A004352", extended=True)

    assert result["ok"] is True
    assert result["extended"] is True
    assert "DecodeVinValuesExtended" in captured["url"]
    assert result["vehicle"]["fueltypeprimary"] == "Gasoline"
    assert result["vehicle"]["displacementl"] == "3.0"


def test_vpic_wmi_decode_returns_wmi_profile(monkeypatch):
    def fake_urlopen(request, timeout=10.0):
        payload = {
            "Results": [
                {
                    "WMI": "WDD",
                    "Name": "MERCEDES-BENZ CARS",
                    "VehicleType": "Passenger Car",
                    "Country": "Germany",
                }
            ]
        }
        return _FakeResponse(payload)

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fake_urlopen)

    result = decode_wmi_vpic("WDD")

    assert result["ok"] is True
    assert result["wmi"] == "WDD"
    assert result["wmi_profile"]["name"] == "MERCEDES-BENZ CARS"


def test_vpic_batch_decode_posts_vins_and_maps_results(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=20.0):
        captured["url"] = request.full_url
        captured["data"] = request.data.decode("utf-8")
        payload = {
            "Results": [
                {
                    "VIN": "1HGCM82633A004352",
                    "Make": "Honda",
                    "Model": "Accord",
                    "ModelYear": "2003",
                }
            ]
        }
        return _FakeResponse(payload)

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fake_urlopen)

    result = decode_vins_vpic_batch([{"identifier": "1HGCM82633A004352", "model_year": 2003}])

    assert result["ok"] is True
    assert "DecodeVINValuesBatch" in captured["url"]
    assert "1HGCM82633A004352" in captured["data"]
    assert result["results_by_vin"]["1HGCM82633A004352"]["vehicle"]["model"] == "Accord"


def test_vpic_decode_rejects_malformed_results_without_crashing(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_lookup.urlopen",
        lambda request, timeout=10.0: _FakeResponse({"Results": ["unexpected-row"]}),
    )

    result = decode_vin_vpic("1HGCM82633A004352")

    assert result["ok"] is False
    assert result["error"] == "vPIC returned a malformed Results payload"


def test_vpic_connection_reset_is_structured_for_fallback(monkeypatch):
    def fail_urlopen(request, timeout=10.0):
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fail_urlopen)

    result = decode_vin_vpic("1HGCM82633A004352")

    assert result["ok"] is False
    assert result["outcome"] == "network_error"
    assert result["retryable"] is True
    assert result["requires_fallback"] is True


def test_vpic_batch_rejects_malformed_results_without_crashing(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_lookup.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"Results": {"VIN": "1HGCM82633A004352"}}),
    )

    result = decode_vins_vpic_batch(["1HGCM82633A004352"])

    assert result["ok"] is False
    assert result["error"] == "vPIC returned a malformed Results payload"
    assert result["results_by_vin"] == {}
