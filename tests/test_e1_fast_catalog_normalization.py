from __future__ import annotations

from io import BytesIO
import json

import pytest

from autostop_manager import catalog_clients as clients
from autostop_manager import config


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    for env in clients.PARTSAPI_METHOD_KEY_ENV_NAMES.values():
        monkeypatch.setenv(env, "synthetic-key")

    def run(operation, payload, **kwargs):
        class Response(BytesIO):
            def __init__(self):
                super().__init__(json.dumps(payload).encode())

        monkeypatch.setattr(clients, "urlopen", lambda *_args, **_kwargs: Response())
        return clients.partsapi_catalog_lookup(operation=operation, **kwargs)

    return run


def car(**overrides):
    return {
        "BODY": "SUV",
        "CAPACITY": "1998/2.0 l",
        "CAR_ID": 123,
        "CAR_NAME": "TEST 200 AWD (111.222)",
        "CAR_TYPES": "PC",
        "ENGINE_TYPE": "Petrol Engine",
        "FULL_MODEL_NAME": "TEST SUV (111) 200 AWD",
        "MAKE_ID": 42,
        "MAKE_NAME": "TEST",
        "MODEL_ID": 43,
        "MODEL_NAME": "SUV (111)",
        "POWER_KW": "150.0000",
        "POWER_PS": "204.0000",
        "YEAR_START": "Sat, 01 Dec 2007 00:00:00 GMT",
        "YEAR_END": "Thu, 01 Dec 2011 00:00:00 GMT",
        **overrides,
    }


@pytest.mark.parametrize("wrapper", [lambda rows: rows, lambda rows: {"data": {"array": rows}}])
def test_live_uppercase_contract_keeps_every_catalog_variant(lookup, wrapper):
    result = lookup(
        "getCars",
        wrapper([car(), car(CAR_ID=124, CAR_NAME="TEST 250 AWD (111.223)")]),
        provider_parameters={"carType": "PC", "makeId": 42, "modelId": 43},
    )
    assert result["outcome"] == "success"
    assert result["record_counts"]["vehicle_profiles"] == 2
    first = result["vehicle_profiles"][0]
    assert first["tecdoc_car_id"] == 123
    assert first["make_id"] == 42 and first["model_id"] == 43
    assert first["make"] == "TEST" and first["model"] == "SUV (111)"
    assert first["modification"] == "TEST 200 AWD (111.222)"
    assert first["full_model_name"] == "TEST SUV (111) 200 AWD"
    assert first["body"] == "SUV"
    assert first["displacement_cc"] == 1998
    assert first["displacement_litres"] == 2.0
    assert first["displacement_litres_raw"] == "2.0"
    assert first["power_kw_from"] == 150 and first["power_hp_from"] == 204
    assert first["engine_type"] == "Petrol Engine"
    assert first["production_date_from"] == "2007-12-01"
    assert first["production_date_to"] == "2011-12-01"
    assert first["vehicle_type"] == "PC"
    assert first["fitment_confirmed"] is False and first["independent_vehicle_confirmation"] is False
    assert first["catalog_candidate_only"] is True
    assert result["catalog_binding"]["fitment_confirmed"] is False


@pytest.mark.parametrize(
    "fields",
    [
        {"carId": 999},
        {"cylinderCapacityCcm": 3000},
        {"powerKw": 200},
        {"powerHp": 300},
        {"carType": "CV"},
        {"makeId": 99},
        {"modelId": 99},
        {"yearOfConstrFrom": "2008-01-01"},
        {"yearOfConstrTo": "2012-01-01"},
    ],
)
def test_new_alias_conflicts_reject_whole_response(lookup, fields):
    result = lookup(
        "getCars", [car(**fields), car(CAR_ID=124)], provider_parameters={"carType": "PC", "makeId": 42, "modelId": 43}
    )
    assert result["ok"] is False
    assert result["outcome"] == "unparsed_response"
    assert result["vehicle_profiles"] == []


@pytest.mark.parametrize("capacity, expected", [("1998/2,0 l", 1998), ("5461/5.5 l", 5461), ("1998", 1998)])
def test_capacity_preserves_cc_and_understands_rounded_liters(capacity, expected):
    profile = clients.extract_partsapi_vehicle_profiles(operation="getCars", payload=[car(CAPACITY=capacity)])[0]
    assert profile["displacement_cc"] == expected


@pytest.mark.parametrize("capacity", ["1998/3.0 l", "NaN", "true", None])
def test_invalid_capacity_is_missing_not_guessed(capacity):
    profile = clients.extract_partsapi_vehicle_profiles(operation="getCars", payload=[car(CAPACITY=capacity)])[0]
    assert "displacement_cc" not in profile


def test_compatible_old_aliases_and_open_end_survive():
    profile = clients.extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[car(carId=123, cylinderCapacityCcm=1998, YEAR_END="")]
    )[0]
    assert profile["displacement_cc"] == 1998
    assert "production_date_to" not in profile


def test_article_oe_block_and_numeric_criteria_stay_with_each_article(lookup):
    rows = [
        {
            "ART_ID": 1,
            "ART_ARTICLE_NR": "TEST-DISC-1",
            "ART_SUP_BRAND": "TEST AFTERMARKET",
            "OEM_NUMBERS": "TEST OEM: A001, TEST OEM: 001, TEST OEM: A001, OTHER OEM: A001",
            "ARTICLE_CRITERIA": "Наружный диаметр [мм]: 375; Толщина диска (мм): 32; Минимальная толщина [мм]: 29,4; Тип: вентилируемый;",
            "CROSSES": [{"brand": "CROSS", "number": "NOT-OE"}],
            "SUPERSEDED": None,
            "SUPERSEDED BY": None,
        },
        {
            "ART_ID": 2,
            "ART_ARTICLE_NR": "TEST-DISC-2",
            "ART_SUP_BRAND": "TEST AFTERMARKET",
            "OEM_NUMBERS": "TEST OEM: A002",
            "ARTICLE_CRITERIA": "Наружный диаметр [мм]: 350",
        },
    ]
    result = lookup("article", rows, part_number="TEST-DISC-1", supplier_id=42)
    assert result["outcome"] == "success" and result["oem_candidates"] == []
    first, second = result["article_candidates"]
    assert [(oe["brand"], oe["part_number"]) for oe in first["oe_references"]] == [
        ("TEST OEM", "A001"),
        ("TEST OEM", "001"),
        ("OTHER OEM", "A001"),
    ]
    assert second["oe_references"][0]["part_number"] == "A002"
    assert all(oe["fitment_confirmed"] is False for oe in first["oe_references"])
    assert first["oe_references"][0]["raw_number"] == "A001"
    assert first["oe_references"][0]["normalized_number"] == "A001"
    assert first["oe_references"][0]["source_operation"] == "getArticle"
    assert first["criteria"][2]["numeric_value"] == 29.4 and first["criteria"][2]["unit"] == "мм"
    assert "numeric_value" not in first["criteria"][3]
    assert second["criteria"][0]["numeric_value"] == 350
    assert first["supersession"] == {"superseded": None, "superseded_by": None}
    assert "NOT-OE" not in str(first["oe_references"])


@pytest.mark.parametrize("value", [None, "", [], {"number": "NOT-A-DECLARED-OE"}, "invalid text"])
def test_malformed_oe_block_never_uses_recursive_number_extraction(value):
    result = clients.extract_partsapi_article_candidates(
        operation="article", payload=[{"ART_ID": 1, "ART_ARTICLE_NR": "TEST", "OEM_NUMBERS": value}]
    )
    assert result[0]["oe_references"] == []


def us_payload(form, vin="A" * 17, **fields):
    row = {
        "VIN": vin,
        "Make": "TEST",
        "Model": "TEST SUV",
        "ErrorCode": "1,8",
        "ErrorText": "Incomplete decode",
        **fields,
    }
    if form == "variables":
        return {"Results": [{"Variable": key, "Value": value} for key, value in row.items()]}
    if form == "flat_results":
        return {"Results": [row], "Message": "Results returned successfully"}
    return {"source": "NHTSA vPIC", **row}


@pytest.mark.parametrize("form", ["variables", "flat_results", "flat"])
def test_us_wrapper_success_is_semantic_partial_and_shared_upstream(lookup, form):
    result = lookup("decodeVINus", us_payload(form), identifier="A" * 17)
    assert result["semantic_status"] == "partial" and result["outcome"] == "partial_result"
    assert result["provider_diagnostics"]["error_codes"] == ["1", "8"]
    assert {"engine", "modification"}.issubset(result["missing_fields"])
    assert result["provenance"]["upstream"] == "nhtsa_vpic"
    assert result["provenance"]["independent_of_direct_vpic"] is False
    assert result["vehicle_profiles"][0]["primary_lineage"] == "nhtsa_vpic"
    assert result["identifier_matches_request"] is True
    assert result["requires_fallback"] is True
    assert result["vehicle_profiles"][0]["fitment_confirmed"] is False


@pytest.mark.parametrize("form", ["variables", "flat_results", "flat"])
def test_us_different_returned_identifier_rejects_profiles(lookup, form):
    result = lookup("decodeVINus", us_payload(form, vin="B" * 17), identifier="A" * 17)
    assert result["ok"] is False and result["outcome"] == "identifier_mismatch"
    assert result["failure_class"] == "provider_identifier_mismatch"
    assert result["vehicle_profiles"] == [] and result["identifier_matches_request"] is False


def test_us_unknown_origin_is_not_claimed_nhtsa(lookup):
    result = lookup("decodeVINus", {"VIN": "A" * 17, "Make": "TEST", "ErrorCode": "8"}, identifier="A" * 17)
    assert result["provenance"]["upstream"] == "unknown"
    assert result["provenance"]["independent_of_direct_vpic"] is None
    assert result["semantic_status"] == "partial"


def test_us_no_vehicle_facts_is_missing_and_not_transport_failure(lookup):
    result = lookup(
        "decodeVINus", {"Results": [{"VIN": "A" * 17, "ErrorCode": "8", "ErrorText": "No data"}]}, identifier="A" * 17
    )
    assert result["semantic_status"] == "missing" and result["outcome"] == "empty_result"
    assert result["vehicle_profiles"] == []
    assert result["attempts"] == [{"attempt": 1, "ok": True}]
    assert result["requires_fallback"] is True


def test_us_unrecognized_nonempty_result_is_not_success(lookup):
    result = lookup("decodeVINus", {"result": {"unrecognized": "response"}}, identifier="A" * 17)
    assert result["outcome"] == "unparsed_response" and result["ok"] is False


def test_us_conflicting_variable_vins_do_not_hide_mismatch(lookup):
    payload = us_payload("variables")
    payload["Results"].append({"Variable": "VIN", "Value": "B" * 17})
    result = lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["outcome"] == "identifier_mismatch" and result["vehicle_profiles"] == []


@pytest.mark.parametrize("returned", ["AAA***AAA", "SHORT", "I" * 17])
def test_us_masked_or_short_vin_cannot_be_full_mismatch_or_confirmation(lookup, returned):
    result = lookup(
        "decodeVINus",
        us_payload(
            "flat_results", vin=returned, ErrorCode="0", ModelYear="2020", EngineModel="TEST-ENGINE", Trim="TEST-TRIM"
        ),
        identifier="A" * 17,
    )
    assert result["identifier_matches_request"] is None
    assert result["semantic_status"] == "partial" and result["outcome"] == "partial_result"
    assert result["requires_fallback"] is True


@pytest.mark.parametrize("form", ["variables", "flat_results"])
def test_us_outer_vin_conflict_is_not_hidden_by_nested_match(lookup, form):
    payload = {"VIN": "B" * 17, "data": us_payload(form)}
    result = lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["identifier_matches_request"] is False
    assert result["outcome"] == "identifier_mismatch" and result["vehicle_profiles"] == []


def test_us_generic_vin_override_binds_request_and_explanatory_text_is_not_error(lookup):
    result = lookup(
        "decodeVINus",
        us_payload(
            "flat_results",
            ErrorCode="0",
            ErrorText="0 - VIN decoded clean",
            ModelYear="2020",
            EngineModel="TEST-ENGINE",
            Trim="TEST-TRIM",
        ),
        provider_parameters={"vin": "A" * 17},
    )
    assert result["identifier_matches_request"] is True
    assert result["semantic_status"] == "complete" and result["outcome"] == "success"
    assert result["provider_diagnostics"]["has_errors"] is False
    assert result["provider_diagnostics"]["has_error_text"] is True
    assert result["vehicle_profiles"][0]["fitment_confirmed"] is False
