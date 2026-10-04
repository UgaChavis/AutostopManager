from datetime import UTC, datetime
import json

import pytest

from autostop_manager.vehicle_identity_inputs import validate_identity_input, validate_identity_item


@pytest.mark.parametrize("year", [True, False, 1, 0, -1, 2004.5, float("inf"), "bad", [], {}, "9999"])
def test_invalid_year_cannot_become_vehicle_fact(year):
    result = validate_identity_input("", {"model_year": year})
    assert result["ok"] is False
    assert "model_year" not in result["context"]
    assert result["errors"][0]["field"] == "model_year"


def test_legacy_year_string_is_explicitly_normalized_but_production_year_stays_separate():
    result = validate_identity_item({"vin": "", "production_year": "2004", "make_display": "Honda"})
    assert result["ok"] is True
    assert result["context"]["production_year"] == 2004
    assert "model_year" not in result["context"]
    assert result["context"]["make"] == "Honda"
    assert result["normalization_notes"] == [{"code": "year_string_normalized", "field": "production_year"}]


@pytest.mark.parametrize("context", [True, False, 3, "private-context", []])
def test_bad_nested_context_is_a_position_error_without_echoing_private_input(context):
    result = validate_identity_item({"identifier": "", "crm_context": context})
    assert result["ok"] is False
    assert any(error["field"] == "crm_context" for error in result["errors"])
    assert "private-context" not in str(result)


def test_nested_compact_profile_and_canonical_alias_conflict_survive_validation():
    result = validate_identity_item(
        {"vehicle_profile_compact": '{"make":"Audi","engine_model":"ENGINE-B"}', "engine": "ENGINE-A"}
    )
    assert result["ok"] is True
    assert result["context"]["make"] == "Audi"
    assert result["context"]["engine"] == "ENGINE-A"
    assert result["context"]["input_alias_conflicts"] == [
        {"field": "engine", "canonical_value": "ENGINE-A", "alias_value": "ENGINE-B", "source": "engine_model"}
    ]
    repeated = validate_identity_input(result["identifier"], result["context"])
    assert repeated["context"]["input_alias_conflicts"] == result["context"]["input_alias_conflicts"]


def test_malformed_alias_cannot_be_hidden_by_a_valid_canonical_value():
    result = validate_identity_item({"make": "Audi", "make_display": {"unexpected": "private"}})
    assert result["ok"] is False
    assert result["errors"] == [{"code": "expected_string", "field": "make_display", "stage": "input_validation"}]
    assert "private" not in str(result)


@pytest.mark.parametrize("confidence", [True, "nan", float("inf"), -0.1, 1.1, [], {}])
def test_nonfinite_or_out_of_range_confidence_cannot_raise_readiness(confidence):
    result = validate_identity_input("", {"source_confidence": confidence})
    assert result["ok"] is False
    assert "source_confidence" not in result["context"]


@pytest.mark.parametrize("date", ["2004-13", "2004-02-30", "04/2004", "private-invalid-date"])
def test_invalid_build_date_is_reported_without_persisting_its_value(date):
    result = validate_identity_input("", {"production_date": date})
    assert result["ok"] is False
    assert result["errors"] == [
        {"code": "invalid_production_date", "field": "production_date", "stage": "input_validation"}
    ]


def test_supported_modification_fields_and_year_boundary_are_preserved():
    year = datetime.now(UTC).year + 1
    context = {
        "model_year": year,
        "production_date": "2004-02",
        "modification": "TEST MODIFICATION",
        "trim": "BASE",
        "series": "TEST SERIES",
        "options": ["A", "A", " B "],
        "transmission_speeds": 6,
    }
    result = validate_identity_input("", context, identifier_type="frame_number")
    assert result["ok"] is True
    assert result["context"]["model_year"] == year
    assert result["context"]["options"] == ["A", "B"]
    assert result["identifier_type"] == "frame_number"
    assert result["context"]["production_date"] == "2004-02"


@pytest.mark.parametrize("field,value", [("make", {}), ("model", []), ("engine", True), ("options", "A,B")])
def test_structures_cannot_be_silently_coerced_to_identity_strings(field, value):
    result = validate_identity_input("", {field: value})
    assert result["ok"] is False
    assert field not in result["context"]


@pytest.mark.parametrize("field", [[], {}])
def test_malformed_alias_metadata_field_is_a_structured_input_error(field):
    result = validate_identity_item(
        {
            "identifier": "",
            "crm_context": {
                "input_alias_conflicts": [
                    {
                        "field": field,
                        "canonical_value": "rejected-value",
                        "alias_value": "other-value",
                        "source": "model_display",
                    }
                ]
            },
        }
    )
    assert result["ok"] is False
    assert result["errors"] == [
        {
            "code": "expected_string",
            "field": "input_alias_conflicts[0].field",
            "stage": "input_validation",
        }
    ]
    assert "rejected-value" not in str(result["errors"])


@pytest.mark.parametrize("empty", [None, "", " \t "])
@pytest.mark.parametrize("carrier", ["crm_context", "vehicle_profile", "vehicle_profile_compact"])
def test_flat_empty_fields_fall_back_to_nonempty_nested_facts(empty, carrier):
    facts = {"make": "Mercedes-Benz", "model": "E200", "model_year": 2010, "engine": "M274.920"}
    if carrier == "crm_context":
        item = {carrier: facts}
    else:
        item = {"crm_context": {carrier: json.dumps(facts) if carrier.endswith("compact") else facts}}
    item.update(dict.fromkeys(facts, empty))
    result = validate_identity_item(item)
    assert result["ok"] is True
    assert all(result["context"][field] == value for field, value in facts.items())


def test_flat_nonempty_fact_overrides_nested_but_bad_types_are_not_fallbacks():
    base = {"crm_context": {"engine": "ENGINE-A"}}
    updated = validate_identity_item({**base, "engine": "ENGINE-B"})
    assert updated["ok"] is True
    assert updated["context"]["engine"] == "ENGINE-B"
    invalid = validate_identity_item({**base, "engine": {}})
    assert invalid["ok"] is False
    assert any(error["field"] == "engine" for error in invalid["errors"])
