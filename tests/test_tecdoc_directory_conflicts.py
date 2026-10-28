"""Catalogue name conflicts are scoped to the reference being selected."""

from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager.tecdoc_vehicle_selection import resolve_tecdoc_vehicle
from tests.test_tecdoc_vehicle_selection import _car, _identity


def _directory(makes: list[dict], models: list[dict]):
    calls = []

    def lookup(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "getMakes":
            return {"ok": True, "payload": {"data": {"array": deepcopy(makes)}}}
        if operation == "getModels":
            return {"ok": True, "payload": {"result": deepcopy(models)}}
        assert operation == "getCars"
        return {"ok": True, "vehicle_profiles": [_car()]}

    return lookup, calls


def _ford(model: str):
    return _identity(make="Ford", model=model, engine="CRCA")


def _conflicting_models():
    return [
        {"modelId": 10, "modelName": "FOCUS"},
        {"modelId": 10, "modelName": "TRANSIT"},
        {"modelId": 20, "modelName": "KUGA"},
    ]


def test_unrelated_model_id_conflict_does_not_hide_no_catalog_vehicle():
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], _conflicting_models())
    result = resolve_tecdoc_vehicle(_ford("Mustang Mach-E"), vehicle_type="PC", call=lookup)
    assert result["status"] == "no_catalog_vehicle"
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_unrelated_conflicting_id_does_not_block_a_distinct_model():
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], _conflicting_models())
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert result["selected_profile"]["tecdoc_car_id"] == 101
    assert calls[-1][0] == "getCars"
    assert calls[-1][1]["provider_parameters"]["modelId"] == 20
    assert result["exact_applicability_confirmed"] is False


@pytest.mark.parametrize("name", ["FOCUS", "TRANSIT"])
def test_selected_conflicting_model_id_blocks_before_getcars(name):
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], _conflicting_models())
    result = resolve_tecdoc_vehicle(_ford(name), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert {row["name"] for row in result["choices"]} == {"FOCUS", "TRANSIT"}
    assert {row["id"] for row in result["choices"]} == {10}
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_selected_conflict_preserves_other_matching_model_alternatives():
    models = [*_conflicting_models(), {"modelId": 30, "modelName": "FOCUS (2012)"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Focus"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert {row["id"] for row in result["choices"]} == {10, 30}
    assert {row["name"] for row in result["choices"]} == {"FOCUS", "TRANSIT", "FOCUS (2012)"}
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_whitespace_and_nbsp_in_same_name_do_not_create_conflict():
    models = [
        {"modelId": 20, "modelName": " Kuga  PLUS "},
        {"modelId": 20, "modelName": "KUGA\u00a0PLUS"},
        {"modelId": 20, "modelName": " kuga\tplus "},
    ]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga Plus"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert calls[-1][0] == "getCars"


def test_unrelated_conflicting_make_id_does_not_block_selected_make():
    makes = [
        {"makeId": 36, "makeName": "Ford"},
        {"makeId": 36, "makeName": "Seat"},
        {"makeId": 2, "makeName": "Toyota"},
    ]
    lookup, calls = _directory(makes, [{"modelId": 20, "modelName": "KUGA"}])
    result = resolve_tecdoc_vehicle(
        _identity(make="Toyota", model="Kuga", engine="CRCA"), vehicle_type="PC", call=lookup
    )
    assert result["status"] == "matched"
    assert calls[1][1]["provider_parameters"]["makeId"] == 2


def test_selected_conflicting_make_id_stops_before_getmodels():
    makes = [{"makeId": 36, "makeName": "Ford"}, {"makeId": 36, "makeName": "Seat"}]
    lookup, calls = _directory(makes, _conflicting_models())
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert {row["name"] for row in result["choices"]} == {"Ford", "Seat"}
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes"]


@pytest.mark.parametrize("requested", ["Kuga", "Focus"])
def test_conflicting_name_aliases_in_one_selected_row_are_ambiguous(requested):
    models = [{"modelId": 20, "modelName": "KUGA", "MOD_CDS_TEXT": "FOCUS"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford(requested), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert {row["name"] for row in result["choices"]} == {"KUGA", "FOCUS"}
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_conflicting_id_aliases_in_one_selected_row_are_ambiguous():
    models = [{"modelId": 20, "MOD_ID": 21, "modelName": "KUGA"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert {row["id"] for row in result["choices"]} == {20, 21}
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


@pytest.mark.parametrize(
    "unrelated",
    [
        {"modelId": 10, "modelName": "FOCUS", "MOD_CDS_TEXT": "TRANSIT"},
        {"modelId": 10, "MOD_ID": 11, "modelName": "FOCUS"},
        {"modelId": 10, "MOD_ID": True, "modelName": "FOCUS"},
    ],
)
def test_unrelated_ambiguous_alias_row_does_not_block_other_models(unrelated):
    models = [unrelated, {"modelId": 20, "modelName": "KUGA"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert calls[-1][1]["provider_parameters"]["modelId"] == 20


def test_selected_invalid_id_alias_fails_closed():
    models = [{"modelId": 20, "MOD_ID": True, "modelName": "KUGA"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_consistent_aliases_and_nbsp_are_not_ambiguous():
    models = [{"modelId": 20, "MOD_ID": "020", "modelName": "KUGA\u00a0PLUS", "MOD_CDS_TEXT": " Kuga  Plus "}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga Plus"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert calls[-1][1]["provider_parameters"]["modelId"] == 20


def test_conflicting_selected_make_aliases_stop_before_models():
    makes = [{"makeId": 36, "MFA_ID": 37, "makeName": "Ford"}]
    lookup, calls = _directory(makes, [{"modelId": 20, "modelName": "KUGA"}])
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert {row["id"] for row in result["choices"]} == {36, 37}
    assert [operation for operation, _ in calls] == ["getMakes"]


def test_bad_name_alias_on_selected_reference_stops_before_getcars():
    models = [{"modelId": 20, "modelName": "KUGA", "MOD_CDS_TEXT": ["not", "a", "name"]}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_unrelated_overlong_id_alias_does_not_break_matching_model():
    models = [{"modelId": "9" * 5000, "modelName": "FOCUS"}, {"modelId": 20, "modelName": "KUGA"}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert calls[-1][1]["provider_parameters"]["modelId"] == 20


def test_malformed_alias_on_a_reused_selected_id_blocks_it():
    models = [
        {"modelId": 20, "modelName": "KUGA"},
        {"modelId": 20, "MOD_ID": True, "modelName": "FOCUS"},
    ]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("selected_id", [20, 21])
def test_nameless_conflicting_model_ids_invalidate_either_selected_id(reverse, selected_id):
    models = [{"modelId": selected_id, "modelName": "KUGA"}, {"modelId": 20, "MOD_ID": 21}]
    if reverse:
        models.reverse()
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["provider_outcome"] == "ambiguous_catalogue_reference_aliases"
    assert result["selected_profile"] is None
    assert any("conflicting_catalogue_reference_ids" in row.get("reference_errors", []) for row in result["choices"])
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("selected_id", [36, 37])
def test_nameless_conflicting_make_ids_invalidate_either_selected_id(reverse, selected_id):
    makes = [{"makeId": selected_id, "makeName": "Ford"}, {"makeId": 36, "MFA_ID": 37}]
    if reverse:
        makes.reverse()
    lookup, calls = _directory(makes, [{"modelId": 20, "modelName": "KUGA"}])
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["provider_outcome"] == "ambiguous_catalogue_reference_aliases"
    assert result["selected_profile"] is None
    assert any("conflicting_catalogue_reference_ids" in row.get("reference_errors", []) for row in result["choices"])
    assert [operation for operation, _ in calls] == ["getMakes"]


@pytest.mark.parametrize("bad_name", [["secret"], {"private": "secret"}, True, " \t\u00a0 ", None])
@pytest.mark.parametrize("kind", ["make", "model"])
def test_nameless_invalid_name_on_selected_reference_stops_navigation(kind, bad_name):
    makes = [{"makeId": 36, "makeName": "Ford"}]
    models = [{"modelId": 20, "modelName": "KUGA"}]
    if kind == "make":
        makes.append({"makeId": 36, "makeName": bad_name})
    else:
        models.append({"modelId": 20, "modelName": bad_name})
    lookup, calls = _directory(makes, models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["provider_outcome"] == "ambiguous_catalogue_reference_aliases"
    assert result["selected_profile"] is None
    assert any("invalid_catalogue_reference_name" in row.get("reference_errors", []) for row in result["choices"])
    assert [operation for operation, _ in calls] == (["getMakes"] if kind == "make" else ["getMakes", "getModels"])
    assert "secret" not in str(result)


@pytest.mark.parametrize("kind", ["make", "model"])
def test_unrelated_nameless_conflicting_or_bad_rows_do_not_block_matching_reference(kind):
    makes = [{"makeId": 36, "makeName": "Ford"}]
    models = [{"modelId": 20, "modelName": "KUGA"}]
    if kind == "make":
        makes.extend([{"makeId": 90, "MFA_ID": 91}, {"makeId": 92, "makeName": ["invalid"]}])
    else:
        models.extend([{"modelId": 90, "MOD_ID": 91}, {"modelId": 92, "modelName": ["invalid"]}])
    lookup, calls = _directory(makes, models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert result["selected_profile"]["tecdoc_car_id"] == 101
    assert calls[-1][0] == "getCars"
    assert calls[-1][1]["provider_parameters"]["modelId"] == 20
    assert result["exact_applicability_confirmed"] is False


def test_unrelated_nameless_conflict_preserves_absent_model_result():
    models = [{"modelId": 20, "modelName": "KUGA"}, {"modelId": 90, "MOD_ID": 91}]
    lookup, calls = _directory([{"makeId": 36, "makeName": "Ford"}], models)
    result = resolve_tecdoc_vehicle(_ford("Mustang Mach-E"), vehicle_type="PC", call=lookup)
    assert result["status"] == "no_catalog_vehicle"
    assert result["selected_profile"] is None
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


@pytest.mark.parametrize("kind", ["make", "model"])
def test_duplicate_id_without_name_or_bad_alias_does_not_block_valid_reference(kind):
    makes = [{"makeId": 36, "makeName": "Ford"}]
    models = [{"modelId": 20, "modelName": "KUGA"}]
    if kind == "make":
        makes.append({"makeId": 36})
    else:
        models.append({"modelId": 20})
    lookup, calls = _directory(makes, models)
    result = resolve_tecdoc_vehicle(_ford("Kuga"), vehicle_type="PC", call=lookup)
    assert result["status"] == "matched"
    assert calls[-1][0] == "getCars"
