from copy import deepcopy

import pytest

from autostop_manager.tecdoc_vehicle_selection import resolve_tecdoc_vehicle


def _identity(**facts):
    return {
        "vehicle_profile": {"make": "Volkswagen", "model": "Touareg", **facts},
        "field_evidence": [
            {"field": field, "value": value, "source": "CRM context"}
            for field, value in {"make": "Volkswagen", "model": "Touareg", **facts}.items()
        ],
    }


def _car(identifier=101, **facts):
    return {"tecdoc_car_id": identifier, "modification": "3.0 TDI", "engine_code": "CRCA", **facts}


def _catalogue(cars, *, engines=None, fail=None, model_name="TOUAREG (7P5, 7P6)"):
    calls = []

    def call(operation, **kwargs):
        calls.append((operation, kwargs))
        if fail == operation:
            return {"ok": False, "outcome": "network_timeout", "error": "synthetic outage"}
        payload = {
            "getMakes": {"data": {"array": [{"MFA_ID": 1, "MFA_BRAND": "VOLKSWAGEN"}]}},
            "getModels": {"result": [{"modelId": "2", "modelName": model_name}]},
        }
        return {
            "ok": True,
            "outcome": "success",
            "payload": payload.get(operation),
            "vehicle_profiles": deepcopy(cars if operation == "getCars" else engines or []),
        }

    return call, calls


def test_route_uses_only_catalogue_navigation_and_keeps_original_identifier():
    identity = _identity(engine="CRCA", production_year=2012)
    identity.update(_source_identifier="A" * 17, normalized_query="A" * 17)
    original = deepcopy(identity)
    callback, calls = _catalogue([_car(production_date_from="2010-01-01", production_date_to="2014-12-01")])
    result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert identity == original
    assert [operation for operation, _ in calls] == ["getMakes", "getModels", "getCars"]
    assert calls[0][1] == {"provider_parameters": {"carType": "PC"}}
    assert calls[1][1] == {"provider_parameters": {"makeId": 1, "carType": "PC", "lang": 16}}
    assert calls[2][1] == {"provider_parameters": {"makeId": 1, "modelId": 2, "carType": "PC"}}
    assert result["requires_exact_identifier_confirmation"] is True
    assert result["exact_applicability_confirmed"] is False


def test_same_vin_prefix_different_years_select_separate_candidates():
    cars = [_car(101, production_year_from=2010, production_year_to=2014), _car(202, production_year_from=2018)]
    selected = []
    for vin, year in [("A" * 8 + "1" * 9, 2012), ("A" * 8 + "2" * 9, 2019)]:
        identity = _identity(engine="CRCA", production_year=year)
        identity["_source_identifier"] = vin
        callback, _ = _catalogue(cars)
        result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
        selected.append(result["selected_profile"]["tecdoc_car_id"])
        assert identity["_source_identifier"] == vin
    assert selected == [101, 202]


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_code", "WRONG"),
        ("model", "Golf"),
        ("transmission", "6AT"),
        ("drivetrain", "FWD"),
        ("modification", "4.2 FSI"),
    ],
)
def test_wrong_material_characteristic_blocks_selection(field, value):
    identity = _identity(engine="CRCA", transmission="8AT", drivetrain="AWD", modification="3.0 TDI")
    callback, _ = _catalogue([_car(**{field: value})])
    result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
    assert result["status"] == "conflict"
    assert result["conflict_scope"] == "catalogue_candidates"
    assert result["selected_profile"] is None


def test_multiple_modifications_return_specific_missing_parameter_and_choices():
    callback, _ = _catalogue([_car(power_hp_from=204, power_hp_to=204), _car(202, power_hp_from=245, power_hp_to=245)])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert "power_hp" in result["missing_fields"]
    assert len(result["choices"]) == 2
    assert result["selected_profile"] is None


def test_numeric_client_photo_facts_select_power_and_displacement():
    cars = [
        _car(power_hp_from=204, power_hp_to=204),
        _car(202, power_hp_from=245, power_hp_to=245, displacement_cc=2967),
    ]
    callback, _ = _catalogue(cars)
    result = resolve_tecdoc_vehicle(
        _identity(engine="CRCA"),
        vehicle_type="PC",
        call=callback,
        independent_context={"power_hp": 245, "displacement_cc": 2967},
    )
    assert result["status"] == "matched"
    assert result["selected_profile"]["tecdoc_car_id"] == 202


@pytest.mark.parametrize(
    "start,end",
    [
        ("Tue, 01 May 2018 00:00:00 GMT", ""),
        ("201805", None),
        ("2018-05", "2020/12"),
        ("2018", "2020"),
        ("2018-05-01", "2020-12-31"),
    ],
)
def test_production_boundaries_support_dates_and_empty_limits(start, end):
    callback, _ = _catalogue([_car(production_date_from=start, production_date_to=end)])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA", production_year=2019), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"


def test_production_date_respects_month_and_day_with_separate_model_year():
    cars = [_car(production_date_from="2018-05-15", production_date_to="2019-12-31")]
    for date, expected in [("2018-05", "matched"), ("2018-05-02", "conflict")]:
        callback, _ = _catalogue(cars)
        result = resolve_tecdoc_vehicle(
            _identity(engine="CRCA", production_date=date, model_year=2020), vehicle_type="PC", call=callback
        )
        assert result["status"] == expected
    assert "model_year" not in cars[0]


@pytest.mark.parametrize("operation", ["getMakes", "getModels", "getCars", "engine_info"])
def test_provider_failure_is_distinct_from_vehicle_conflict(operation):
    callback, calls = _catalogue([{"tecdoc_car_id": 101}], fail=operation)
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "provider_failed"
    assert result["provider_outcome"] == "network_timeout"
    assert result["conflicting_fields"] == []
    assert calls[-1][0] == operation


def test_local_rule_and_same_catalogue_do_not_establish_customer_facts():
    identity = _identity()
    identity["field_evidence"] = [
        {"field": field, "value": value, "source": source}
        for field, value, source in [
            ("make", "Volkswagen", "local WMI hint"),
            ("model", "Touareg", "PartsAPI VINdecode"),
        ]
    ]
    callback, calls = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
    assert result["status"] == "needs_vehicle_context"
    assert result["missing_fields"] == ["make", "model"]
    assert calls == []


def test_independent_context_can_supply_make_model_without_decoder_evidence():
    callback, _ = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(
        {"vehicle_profile": {"make": "WRONG", "model": "WRONG"}},
        vehicle_type="PC",
        call=callback,
        independent_context={"vehicle_profile": {"make": "Volkswagen", "model": "Touareg", "engine": "CRCA"}},
    )
    assert result["status"] == "matched"


def test_independent_engine_conflict_blocks_even_if_catalogue_field_missing():
    identity = _identity(engine="CRCA")
    identity["field_evidence"].append({"field": "engine", "value": "WRONG", "source": "NHTSA vPIC"})
    callback, calls = _catalogue([{"tecdoc_car_id": 101}])
    result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
    assert result["status"] == "conflict"
    assert result["conflict_scope"] == "independent_vehicle"
    assert result["conflicting_fields"][0]["field"] == "engine"
    assert calls == []


def test_optional_engine_info_keeps_variants_separate():
    callback, calls = _catalogue([{"tecdoc_car_id": 101}], engines=[{"engine_code": "WRONG"}, {"engine_code": "CRCA"}])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert result["selected_profile"]["engine_code"] == "CRCA"
    assert calls[-1] == ("engine_info", {"type_id": 101, "vehicle_type": "PC", "lang_id": 16})


def test_distinct_model_families_are_not_matched_by_prefix():
    callback, calls = _catalogue([_car()], model_name="Touareg Cross")
    result = resolve_tecdoc_vehicle(_identity(), vehicle_type="PC", call=callback)
    assert result["status"] == "no_catalog_vehicle"
    assert [operation for operation, _ in calls] == ["getMakes", "getModels"]


def test_single_catalogue_variant_without_independent_technical_facts_is_not_selected():
    callback, _ = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(_identity(), vehicle_type="PC", call=callback)
    assert result["status"] == "needs_vehicle_context"
    assert result["vehicle_profiles"][0]["tecdoc_car_id"] == 101
    assert result["selected_profile"] is None
    assert "engine" in result["missing_fields"]


def test_missing_known_catalogue_characteristic_requires_confirmation():
    callback, _ = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA", transmission="8AT"), vehicle_type="PC", call=callback)
    assert result["status"] == "needs_vehicle_context"
    assert result["missing_fields"] == ["transmission"]
    assert result["selected_profile"] is None


def test_engine_marketing_description_and_engine_code_are_not_a_conflict():
    callback, _ = _catalogue([_car(displacement_cc=2967, fuel_type="diesel")])
    result = resolve_tecdoc_vehicle(_identity(engine="3.0 TDI"), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert result["selected_profile"]["engine_code"] == "CRCA"
    assert result["exact_applicability_confirmed"] is False


def test_catalogue_drive_type_is_checked_against_independent_drivetrain():
    callback, _ = _catalogue([_car(drive_type="FWD")])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA", drivetrain="AWD"), vehicle_type="PC", call=callback)
    assert result["status"] == "conflict"
    assert result["conflicting_fields"][0]["field"] == "drivetrain"


def test_motocycle_vehicle_type_matches_existing_catalogue_contract():
    callback, calls = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="Motorcycle", call=callback)
    assert result["status"] == "matched"
    assert calls[0][1]["provider_parameters"]["carType"] == "Motorcycle"


def test_dry_run_does_not_mean_provider_failure_or_vehicle_conflict():
    result = resolve_tecdoc_vehicle(
        _identity(engine="CRCA"),
        vehicle_type="PC",
        call=lambda *args, **kwargs: {"ok": True, "dry_run": True, "outcome": "configured_unverified"},
    )
    assert result["status"] == "not_checked"
    assert result["conflicting_fields"] == []


def test_call_digests_do_not_expose_payload_or_urls():
    callback, _ = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert all(set(row) == {"operation", "ok", "outcome", "failure_class", "dry_run"} for row in result["calls"])


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True, {"value": 245}, [245], -10])
def test_invalid_numeric_independent_context_cannot_select_a_candidate(invalid):
    callback, calls = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(
        _identity(engine="CRCA"),
        vehicle_type="PC",
        call=callback,
        independent_context={"power_hp": invalid},
    )
    assert result["status"] == "needs_vehicle_context"
    assert "power_hp" in result["invalid_context_fields"]
    assert result["selected_profile"] is None
    assert calls == []


def test_duplicate_candidate_ids_do_not_force_selection_or_repeat_known_questions():
    callback, _ = _catalogue([_car(), _car(202)])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "ambiguous_vehicle_modification"
    assert result["selected_profile"] is None
    assert "engine" not in result["missing_fields"]


def test_engine_description_cannot_hide_conflict_between_two_exact_codes():
    identity = _identity(engine="3.0 TDI")
    identity["field_evidence"].extend(
        [
            {"field": "engine", "value": "CRCA", "source": "CRM context"},
            {"field": "engine", "value": "WRONG", "source": "NHTSA vPIC"},
        ]
    )
    callback, calls = _catalogue([_car()])
    result = resolve_tecdoc_vehicle(identity, vehicle_type="PC", call=callback)
    assert result["status"] == "conflict"
    assert calls == []


def test_marketing_engine_name_does_not_confirm_a_known_exact_engine_code():
    callback, calls = _catalogue(
        [{"tecdoc_car_id": 101, "engine": "3.0 TDI"}],
        engines=[{"engine_code": "CRCA"}],
    )
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert calls[-1][0] == "engine_info"


@pytest.mark.parametrize("field,value", [("make_id", 999), ("model_id", 999), ("vehicle_type", "CV")])
def test_getcars_explicit_reference_mismatch_is_a_source_error(field, value):
    callback, _ = _catalogue([_car(**{field: value})])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "provider_failed"
    assert result["provider_outcome"] == "catalogue_reference_mismatch"
    assert result["conflicting_fields"] == []
    assert result["selected_profile"] is None


def test_getengine_cannot_replace_returned_wrong_vehicle_id_with_requested_id():
    callback, _ = _catalogue([{"tecdoc_car_id": 101}], engines=[{"engine_code": "CRCA", "tecdoc_car_id": 999}])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "provider_failed"
    assert result["provider_outcome"] == "catalogue_reference_mismatch"
    assert result["selected_profile"] is None


def test_single_power_value_is_not_an_unbounded_power_range():
    callback, _ = _catalogue([_car(101, power_hp_from=204), _car(202, power_hp_from=245)])
    result = resolve_tecdoc_vehicle(
        _identity(engine="CRCA"), vehicle_type="PC", call=callback, independent_context={"power_hp": 245}
    )
    assert result["status"] == "matched"
    assert result["selected_profile"]["tecdoc_car_id"] == 202


@pytest.mark.parametrize("known,returned", [("Touareg II", "Touareg III"), ("Touareg (7P)", "Touareg (CR7)")])
def test_explicit_customer_generation_cannot_be_replaced_by_another_catalogue_generation(known, returned):
    callback, _ = _catalogue([_car(model=returned)], model_name=returned)
    result = resolve_tecdoc_vehicle(_identity(model=known, engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "conflict"
    assert result["conflict_scope"] == "catalogue_candidates"
    assert result["selected_profile"] is None
    assert any(row["field"] == "model" for row in result["conflicting_fields"])


def test_generic_customer_model_can_navigate_a_catalogue_generation_as_a_candidate():
    callback, _ = _catalogue([_car(model="Touareg III")], model_name="Touareg III")
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=callback)
    assert result["status"] == "matched"
    assert result["selected_profile"]["model"] == "Touareg III"
    assert result["exact_applicability_confirmed"] is False
