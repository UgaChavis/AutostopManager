"""VINdecode group references are useful catalog candidates, not exact VIN facts."""

from __future__ import annotations

import copy
from io import BytesIO
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import catalog_clients as catalog
from autostop_manager import config
from autostop_manager.automotive_contracts import binding
from autostop_manager.catalog_presentation import present_catalog

REQUESTED = "A" * 17
REPRESENTATIVE = "B" * 17


def vehicle(**fields):
    return {
        "vin": REPRESENTATIVE,
        "carId": "123",
        "carType": "PC",
        "manuName": "SYNTHETIC",
        "modelName": "Example",
        "motorCodes": "SYNTHETIC-ENGINE",
        **fields,
    }


class Response(BytesIO):
    status = 200

    def __init__(self, payload):
        self.headers = {"Content-Type": "application/json"}
        super().__init__(json.dumps(payload).encode())


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    for name in set(catalog.PARTSAPI_METHOD_KEY_ENV_NAMES.values()):
        monkeypatch.delenv(name, raising=False)
    for method in ("VINdecode", "decodeVINus"):
        monkeypatch.setenv(catalog.PARTSAPI_METHOD_KEY_ENV_NAMES[method], "synthetic-test-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls = []

    def run(payload, *, operation="vin_decode", detail="full"):
        def respond(request, **_kwargs):
            parameters = parse_qs(urlsplit(request.full_url).query)
            calls.append({"method": parameters["method"][0], "vin": parameters.get("vin", [None])[0]})
            return Response(payload)

        monkeypatch.setattr(catalog, "urlopen", respond)
        result = catalog.partsapi_catalog_lookup(operation=operation, identifier=REQUESTED, max_attempts=1)
        return present_catalog(result, detail)

    run.calls = calls
    return run


def test_group_reference_keeps_requested_binding_and_candidate_scope(lookup):
    result = lookup(vehicle())

    assert lookup.calls == [{"method": "VINdecode", "vin": REQUESTED}]
    assert result["ok"] is True
    assert result["outcome"] == "group_match"
    assert result["identifier_matches_request"] is False
    assert result["input_binding"] == binding(REQUESTED, "vin")
    assert result["input_binding"] != binding(REPRESENTATIVE, "vin")
    assert result["binding_kind"] == "provider_group_reference"
    assert result["identifier_semantics"] == "group_representative"
    assert result["requires_exact_identifier_confirmation"] is True
    profile = result["vehicle_profiles"][0]
    assert profile["input_binding"] == result["input_binding"]
    assert profile["catalog_candidate_only"] is True
    assert profile["independent_vehicle_confirmation"] is False
    assert profile["fitment_confirmed"] is False
    assert profile["provider_identifier_is_vehicle_confirmation"] is False
    assert profile["provenance"] == {
        "provider": "partsapi_ru",
        "primary_lineage": "partsapi_ru",
        "semantics_basis": "owner_clarification",
    }
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is True


def test_exact_echo_still_succeeds_without_group_semantics(lookup):
    result = lookup(vehicle(vin=REQUESTED))

    assert result["ok"] is True
    assert result["outcome"] == "success"
    assert result["identifier_matches_request"] is True
    assert result["input_binding"] == binding(REQUESTED, "vin")
    assert result.get("binding_kind") != "provider_group_reference"
    assert catalog.partsapi_is_group_reference(result) is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is True


def test_summary_preserves_binding_provenance_and_normalized_profile(lookup):
    payload = vehicle(kpp="Automatic")
    full = lookup(payload)
    summary = lookup(payload, detail="summary")

    for field in ("outcome", "identifier_matches_request", "binding_kind", "identifier_semantics", "input_binding"):
        assert summary[field] == full[field]
    assert len(summary["vehicle_profiles"]) == len(full["vehicle_profiles"]) == 1
    for field in (
        "provider",
        "source_operation",
        "identifier_matches_request",
        "binding_kind",
        "identifier_semantics",
        "input_binding",
        "catalog_candidate_only",
        "independent_vehicle_confirmation",
        "fitment_confirmed",
        "provenance",
        "engine",
        "transmission",
        "tecdoc_car_id",
    ):
        assert summary["vehicle_profiles"][0][field] == full["vehicle_profiles"][0][field]
    assert summary["vehicle_profiles"][0]["engine"] == "SYNTHETIC-ENGINE"
    assert summary["vehicle_profiles"][0]["transmission"] == "Automatic"
    assert summary["vehicle_profiles"][0]["tecdoc_car_id"] == "123"
    assert "payload" not in summary
    assert catalog.partsapi_identifier_allows_candidate_lookup(summary) is True


@pytest.mark.parametrize("returned", [None, "B" * 16, {"value": REPRESENTATIVE}])
def test_missing_short_or_malformed_vin_is_not_a_group_reference(lookup, returned):
    result = lookup(vehicle(vin=returned))

    assert result["outcome"] != "group_match"
    assert catalog.partsapi_is_group_reference(result) is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False


@pytest.mark.parametrize(
    "payload",
    [
        vehicle(VIN=REQUESTED),
        vehicle(TYPE_ID="456"),
        [vehicle(vin=REQUESTED), vehicle()],
    ],
)
def test_conflicting_aliases_and_mixed_exact_foreign_records_are_rejected(lookup, payload):
    result = lookup(payload)

    assert result["ok"] is False
    assert result["outcome"] != "group_match"
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False


@pytest.mark.parametrize("second", [vehicle(carId="456"), vehicle(modelName="Other example")])
def test_multiple_groups_are_retained_but_cannot_choose_a_modification(lookup, second):
    result = lookup([vehicle(), second])

    assert len(result["vehicle_profiles"]) == 2
    assert {profile["tecdoc_car_id"] for profile in result["vehicle_profiles"]} == {"123", second["carId"]}
    assert all(profile["catalog_candidate_only"] is True for profile in result["vehicle_profiles"])
    assert catalog.partsapi_is_group_reference(result) is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False


def test_decode_vin_us_keeps_strict_foreign_identifier_rejection(lookup):
    result = lookup(
        {"Results": [{"VIN": REPRESENTATIVE, "Make": "SYNTHETIC", "Model": "Example", "ErrorCode": "0"}]},
        operation="decodeVINus",
    )

    assert result["ok"] is False
    assert result["outcome"] == "identifier_mismatch"
    assert result["identifier_matches_request"] is False
    assert catalog.partsapi_is_group_reference(result) is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False


@pytest.mark.parametrize(
    "forgery",
    [
        "provider",
        "operation",
        "invalid_binding",
        "non_vin_binding",
        "profile_binding",
        "literal_match",
        "provenance_provider",
        "primary_lineage",
    ],
)
def test_untrusted_metadata_cannot_open_the_group_candidate_gate(lookup, forgery):
    call = copy.deepcopy(lookup(vehicle()))
    profile = call["vehicle_profiles"][0]
    if forgery in {"provider", "operation"}:
        call[forgery] = "untrusted"
    elif forgery == "invalid_binding":
        call["input_binding"] = profile["input_binding"] = {
            "version": 1,
            "identifier_kind": "vin",
            "identifier_sha256": "invalid",
        }
    elif forgery == "non_vin_binding":
        call["input_binding"]["identifier_kind"] = "frame_number"
        profile["input_binding"] = copy.deepcopy(call["input_binding"])
    elif forgery == "profile_binding":
        profile["input_binding"] = binding(REPRESENTATIVE, "vin")
    elif forgery == "literal_match":
        call["identifier_matches_request"] = True
    elif forgery == "provenance_provider":
        profile["provenance"]["provider"] = "untrusted"
    else:
        profile["provenance"]["primary_lineage"] = "untrusted"

    assert catalog.partsapi_is_group_reference(call) is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(call) is False
