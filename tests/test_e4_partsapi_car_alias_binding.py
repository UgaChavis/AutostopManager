from __future__ import annotations

from io import BytesIO
import json

import pytest

from autostop_manager import catalog_clients as clients


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(clients, "load_runtime_env", lambda: None)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv(clients.PARTSAPI_METHOD_KEY_ENV_NAMES["getCars"], "synthetic-key")

    def run(rows):
        calls = []

        def respond(*_args, **_kwargs):
            calls.append("getCars")
            return BytesIO(json.dumps(rows).encode())

        monkeypatch.setattr(clients, "urlopen", respond)
        result = clients.partsapi_catalog_lookup(
            operation="getCars", provider_parameters={"carType": "PC", "makeId": 42, "modelId": 43}
        )
        assert calls == ["getCars"]
        return result

    return run


@pytest.mark.parametrize(
    "fields",
    [
        {"engineType": "Petrol Engine", "ENGINE_TYPE": "Diesel Engine"},
        {"fuelType": "Petrol", "FUEL_TYPE": "Diesel"},
        {"engineType": "Petrol Engine", "engine_type": "Petrol Engine", "ENGINE_TYPE": "Diesel Engine"},
        {"fuelType": "Petrol", "fuel_type": "Petrol", "FUEL_TYPE": "Diesel"},
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_engine_or_fuel_aliases_reject_entire_getcars_response(lookup, fields, reverse):
    rows = [{"CAR_ID": 123, **fields}, {"CAR_ID": 124, "ENGINE_TYPE": "Petrol Engine", "FUEL_TYPE": "Petrol"}]
    if reverse:
        rows.reverse()

    result = lookup(rows)

    assert result["ok"] is False
    assert result["outcome"] == "unparsed_response"
    assert result["failure_class"] == "adapter_unparsed_response"
    assert result["vehicle_profiles"] == []
    assert result["record_counts"]["vehicle_profiles"] == 0
    assert result["requires_fallback"] is True


def test_compatible_engine_and_fuel_aliases_preserve_existing_text_and_candidate_scope(lookup):
    result = lookup(
        [
            {
                "CAR_ID": 123,
                "engineType": "  Diesel Engine  ",
                "engine_type": "diesel engine",
                "TYP_ENGINE_DES": "DIESEL ENGINE",
                "ENGINE_TYPE": "Diesel-Engine",
                "fuelType": "  Diesel  ",
                "fuel_type": "diesel",
                "TYP_FUEL_DES": "DIESEL",
                "FUEL_TYPE": "Diesel.",
            }
        ]
    )

    assert result["ok"] is True
    assert result["outcome"] == "success"
    profile = result["vehicle_profiles"][0]
    assert profile["engine_type"] == "Diesel Engine"
    assert profile["fuel_type"] == "Diesel"
    assert profile["catalog_candidate_only"] is True
    assert profile["requires_exact_identifier_confirmation"] is True
    assert profile["independent_vehicle_confirmation"] is False
    assert profile["fitment_confirmed"] is False


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"ENGINE_TYPE": "PeTrOl Engine"}, {"engine_type": "PeTrOl Engine"}),
        ({"engineType": "PeTrOl Engine"}, {"engine_type": "PeTrOl Engine"}),
        ({"FUEL_TYPE": "PeTrOl"}, {"fuel_type": "PeTrOl"}),
        ({"fuelType": "PeTrOl"}, {"fuel_type": "PeTrOl"}),
        ({}, {}),
    ],
)
def test_single_or_absent_engine_fuel_aliases_keep_existing_normalization(lookup, fields, expected):
    result = lookup([{"CAR_ID": 123, **fields}])

    assert result["ok"] is True
    assert result["outcome"] == "success"
    profile = result["vehicle_profiles"][0]
    assert {field: profile[field] for field in ("engine_type", "fuel_type") if field in profile} == expected
