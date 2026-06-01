from __future__ import annotations

import json

from autostop_manager.vin_lookup import decode_vin_vpic, decode_vins_vpic_batch, decode_wmi_vpic


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
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
