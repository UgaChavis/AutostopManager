from __future__ import annotations

import json
import socket

import pytest

from autostop_manager import vehicle_identity as identity
from autostop_manager.vehicle_identity_policy import identity_allows_lookup, identity_has_blocking_conflicts


@pytest.fixture(autouse=True)
def isolate_providers(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Semantic controls cannot connect to providers")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(identity, "catalog_provider_status", lambda: {"providers": []})


def synthetic_identifier() -> str:
    return "WBA" + "0" * 14


def provider(identifier: str, **fields):
    return {
        "ok": True,
        "error_code": "0",
        "source": "NHTSA vPIC",
        "vin": identifier,
        "vehicle": {"vin": identifier, "make": "BMW", "model": "3 Series", **fields},
    }


def decode(context=None, result=None, *, identifier=None, wmi=None, **kwargs):
    return identity.decode_vehicle_identity(
        synthetic_identifier() if identifier is None else identifier,
        crm_context=context,
        live_vpic=False,
        live_wmi=False,
        vpic_result=result,
        wmi_result=wmi,
        **kwargs,
    )


@pytest.mark.parametrize("origin", ["caller", "local_rule"])
@pytest.mark.parametrize(
    "field,key,left,right",
    [
        ("make", "make", "BMW", "Audi"),
        ("model", "model", "3 Series", "5 Series"),
        ("model_year", "modelyear", 2018, 2019),
        ("engine", "enginemodel", "N20B20", "N52B25"),
        ("transmission", "transmissionstyle", "Manual", "Automatic"),
        ("transmission_speeds", "transmissionspeeds", 6, "8"),
        ("drivetrain", "drivetype", "FWD", "RWD"),
        ("trim", "trim", "Sport", "Luxury"),
        ("series", "series", "F30", "E90"),
    ],
)
def test_disagreement_omits_canonical_field_and_keeps_family_research(monkeypatch, origin, field, key, left, right):
    context = {"make": "BMW", "model": "3 Series"}
    if origin == "caller":
        context[field] = left
    else:
        fields = {"make": "BMW", "model": "3 Series", field: left}
        rule = identity.PlatformRule("controlled_rule", "^WBA", "vin_prefix", fields, "synthetic control", 0.8)
        monkeypatch.setattr(identity, "_matching_platform_rule", lambda _identifier: rule)
    result = decode(context, provider(synthetic_identifier(), **{key: right}))
    assert result["field_statuses"][field]["status"] == "disputed"
    assert field not in result["vehicle_profile"]
    assert any(conflict["field"] == field for conflict in result["conflicts"])
    assert identity_allows_lookup(result, "family") is True
    assert identity_allows_lookup(result, "vehicle") is False
    assert result["confidence_label"] != "high"
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("make", "Škoda", "Skoda"),
        ("make", "VW", "Volkswagen"),
        ("make", "Toyota Motor Corporation", "Toyota"),
        ("engine", "M274.920", "M274920"),
        ("engine", "R06A", "R06A 658cc gasoline"),
        ("engine", "K9K", "K9K 1.5 diesel"),
        ("transmission", "Automatic", "6AT"),
        ("transmission", "АКПП", "6-speed Automatic"),
        ("transmission", "Manual", "6MT"),
        ("drivetrain", "4WD", "4WD/4-Wheel Drive/4x4"),
        ("market", "EU", "Europe"),
    ],
)
def test_benign_semantic_aliases(field, left, right):
    assert identity.identity_values_agree(field, left, right) is True


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("make", "Škoda", "Volkswagen"),
        ("model", "Corolla", "Corolla Cross"),
        ("engine", "M274.920", "M274.910"),
        ("engine", "R06A", "K6A"),
        ("engine", "2.0L diesel", "2.0L gasoline"),
        ("engine", "EURO4 M274.920", "EURO4 M274.910"),
        ("transmission", "6AT", "8AT"),
        ("transmission", "Automatic", "Manual"),
        ("transmission", "DQ200", "DQ250"),
        ("market", "Japan", "Japanish"),
        ("market", "Europe", "North America"),
    ],
)
def test_incompatible_semantic_values(field, left, right):
    assert identity.identity_values_agree(field, left, right) is False


@pytest.mark.parametrize("caller_gears,provider_gears,disputed", [(6, "6", False), (8, "6", True)])
def test_transmission_style_comparison_uses_source_gear_count(caller_gears, provider_gears, disputed):
    result = decode(
        {"make": "BMW", "model": "3 Series", "transmission": f"{caller_gears}AT"},
        provider(synthetic_identifier(), transmissionstyle="Automatic", transmissionspeeds=provider_gears),
    )
    assert (result["field_statuses"]["transmission"]["status"] == "disputed") is disputed
    assert identity_has_blocking_conflicts(result, "vehicle") is disputed


def test_provider_vs_wmi_conflict_is_not_arbitrated_by_merge_order():
    result = decode(
        {"make": "BMW", "model": "3 Series"},
        provider(synthetic_identifier()),
        wmi={"ok": True, "wmi": "WBA", "wmi_profile": {"make": "Audi", "country": "Germany"}},
    )
    assert result["field_statuses"]["make"]["status"] == "disputed"
    assert "make" not in result["vehicle_profile"]
    assert result["vehicle_profile"]["manufacturer_country"] == "Germany"
    assert "plant_country" not in result["vehicle_profile"]
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


def test_family_alternatives_keep_make_and_model_from_same_source():
    identifier = "WAU" + "ZZZ4H" + "A" * 9
    result = decode(
        {"make": "Toyota", "model": "Camry"}, provider(identifier, make="Audi", model="A8"), identifier=identifier
    )
    assert {(row["make"], row["model"]) for row in result["family_candidates"]} == {("Toyota", "Camry"), ("Audi", "A8")}
    assert "make" not in result["vehicle_profile"]
    assert "model" not in result["vehicle_profile"]
    assert result["lookup_plan"]["routing_basis"] == "reconciled_identity"


def test_year_hint_echo_is_caller_derived_and_not_independent_support():
    source = provider(synthetic_identifier(), modelyear=2014)
    source["model_year_hint_requested"] = 2014
    result = decode({"model_year": 2014}, source)
    assert result["field_statuses"]["model_year"]["status"] == "candidate"
    evidence = next(
        row for row in result["field_evidence"] if row["field"] == "model_year" and row["source"] == "NHTSA vPIC"
    )
    assert evidence["independent"] is False
    assert evidence["depends_on"] == ["caller.model_year"]
    assert result["parts_lookup_readiness"]["ready_for_modification_lookup"] is False


def test_year_without_hint_is_separate_from_production_and_build_date():
    result = decode(
        {"model_year": 2019, "production_year": 2018, "production_date": "2018-11-30"},
        provider(synthetic_identifier(), modelyear=2019),
    )
    assert result["field_statuses"]["model_year"]["status"] == "supported"
    assert result["vehicle_profile"]["production_year"] == 2018
    assert result["vehicle_profile"]["production_date"] == "2018-11-30"
    assert not any(row["field"] in {"model_year", "production_year", "production_date"} for row in result["conflicts"])


def test_clean_source_year_is_checked_against_na_vin_code_without_caller_hint():
    identifier = "1C4" + "RJFCT9" + "CC" + "0" * 6
    result = decode(
        {"make": "Jeep", "model": "Grand Cherokee"},
        provider(identifier, make="Jeep", model="Grand Cherokee", modelyear=2013),
        identifier=identifier,
    )
    assert result["field_statuses"]["model_year"]["status"] == "disputed"
    assert "model_year" not in result["vehicle_profile"]
    assert any(row["code"] == "vin_model_year_disagreement" for row in result["conflicts"])
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


def test_production_year_and_build_date_must_describe_the_same_build_year():
    result = decode({"make": "BMW", "model": "3 Series", "production_year": 2018, "production_date": "2019-01"})
    assert result["field_statuses"]["production_year"]["status"] == "disputed"
    assert result["field_statuses"]["production_date"]["status"] == "disputed"
    assert "production_year" not in result["vehicle_profile"]
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


@pytest.mark.parametrize("error_code", [None, "", "5", "0"])
def test_variant_fields_require_reported_clean_diagnostics(error_code):
    source = provider(synthetic_identifier(), enginemodel="N20B20", modelyear=2018)
    source["error_code"] = error_code
    result = decode({"make": "BMW", "model": "3 Series"}, source)
    if error_code == "0":
        assert result["field_statuses"]["engine"]["status"] == "supported"
        assert not result["provider_errors"]
    else:
        assert "engine" not in result["vehicle_profile"]
        assert result["field_statuses"]["make"]["status"] == "candidate"
        assert result["status"] == "partial"
        assert result["provider_errors"][0]["code"] == "provider_partial_evidence"
        assert result["confidence_label"] != "high"
        assert identity_allows_lookup(result, "family")
        assert not identity_allows_lookup(result, "vehicle")


def test_country_never_becomes_a_sales_market_or_imposes_na_vin_rules():
    identifier = "WVW" + "ZZZAUZ" + "FP" + "0" * 6
    result = decode(
        {"make": "Volkswagen", "model": "Golf", "model_year": 2014, "market": "Europe"},
        provider(identifier, make="Volkswagen", model="Golf", plantcountry="UNITED STATES (USA)"),
        identifier=identifier,
    )
    assert result["vehicle_profile"]["market"] == "Europe"
    assert result["vehicle_profile"]["plant_country"] == "UNITED STATES (USA)"
    assert not any(row["field"] in {"vin_check_digit", "model_year"} for row in result["conflicts"])


@pytest.mark.parametrize(
    "context",
    [
        {"source_confidence": 0.99},
        {"source_confidence": 0.99, "oem_notes": "context only"},
        {"source_summary": "context only"},
    ],
)
def test_metadata_does_not_raise_identity_score(context):
    identifier = "WAU" + "ZZZ4H" + "A" * 9
    assert decode(context, identifier=identifier)["confidence"] == decode(identifier=identifier)["confidence"]


@pytest.mark.parametrize(
    "alias,canonical,left,right",
    [
        ("engine_model", "engine", "N20B20", "N52B25"),
        ("make_display", "make", "BMW", "Audi"),
        ("gearbox_model", "transmission", "Automatic", "Manual"),
    ],
)
def test_caller_alias_conflicts_survive_validation(alias, canonical, left, right):
    result = decode({"make": "BMW", "model": "3 Series", canonical: left, alias: right})
    assert result["field_statuses"][canonical]["status"] == "disputed"
    assert canonical not in result["vehicle_profile"]
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


@pytest.mark.parametrize("bound_echo", [None, "foreign", "metadata_spoof"])
def test_unbound_provider_cannot_promote_foreign_identity(bound_echo):
    source = provider(synthetic_identifier(), enginemodel="N20B20")
    if bound_echo is None:
        source.pop("vin")
        source["vehicle"].pop("vin")
    else:
        foreign = "WAU" + "1" * 14
        source["vehicle"]["vin"] = foreign
        if bound_echo == "metadata_spoof":
            source["identifier_binding"] = {"status": "exact", "verified": True}
    result = decode({"make": "BMW", "model": "3 Series"}, source)
    assert result["status"] == "partial"
    assert "engine" not in result["vehicle_profile"]
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")
    assert source.get("vehicle", {}).get("vin", "absent") not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("make", {}),
        ("model", []),
        ("enginemodel", True),
        ("modelyear", True),
        ("modelyear", "2000oops"),
        ("enginehp", float("nan")),
        ("enginehp", 10**400),
        ("transmissionstyle", 8),
    ],
)
def test_malformed_injected_provider_is_a_typed_partial_failure(field, value):
    result = decode({"make": "BMW", "model": "3 Series"}, provider(synthetic_identifier(), **{field: value}))
    assert result["status"] == "partial"
    assert result["evidence_sources"][-1]["outcome"] == "adapter_malformed_payload"
    assert all(not isinstance(row.get("value"), (dict, bool)) for row in result["field_evidence"])


@pytest.mark.parametrize("identifier_type", ["auto", "vin"])
def test_illegal_iso_vin_syntax_cannot_be_high_or_vehicle_ready(identifier_type):
    identifier = "WAU" + "ZZZ4H" + "I" * 9
    result = decode(
        {"make": "Audi", "model": "A8", "source_confidence": 0.99},
        identifier=identifier,
        identifier_type=identifier_type,
    )
    assert result["identifier_validation"]["valid_for_vehicle_lookup"] is False
    assert result["confidence_label"] != "high"
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


def test_na_checksum_failure_blocks_vehicle_but_keeps_family():
    identifier = "1C4" + "RJFCT0" + "CC" + "0" * 6
    result = decode({"make": "Jeep", "model": "Grand Cherokee", "source_confidence": 0.99}, identifier=identifier)
    assert result["diagnostics"]["check_digit"]["status"] == "fail"
    assert result["identifier_validation"]["valid_for_vehicle_lookup"] is False
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


def test_partial_vin_retains_candidate_family_without_variant_confirmation():
    identifier = "WBA" + "0" * 6 + "*" * 8
    result = decode(
        {"make": "BMW", "model": "3 Series"},
        provider(identifier, enginemodel="N20B20", modelyear=2018),
        identifier=identifier,
    )
    assert result["field_statuses"]["make"]["status"] == "candidate"
    assert "engine" not in result["vehicle_profile"]
    assert identity_allows_lookup(result, "family")
    assert not identity_allows_lookup(result, "vehicle")


def test_missing_fields_are_reported_without_filling_from_unrelated_metadata():
    result = decode({"make": "BMW", "model": "3 Series", "source_summary": "available"})
    assert {"model_year", "production_date", "engine", "transmission", "modification", "options"}.issubset(
        result["missing_fields"]
    )
    assert result["field_statuses"]["model_year"]["status"] == "missing"
    assert result["parts_lookup_readiness"]["ready_for_modification_lookup"] is False


def test_provenance_preserves_raw_normalized_values_and_dependencies():
    result = decode({"make": "Volkskwagen", "model": "Golf", "production_year": 2018})
    make = next(row for row in result["provenance"]["make"] if row["source"] == "CRM context")
    assert make["raw_value"] == "Volkskwagen"
    assert make["value"] == "Volkswagen"
    assert make["normalization"] == {"method": "make_alias_unicode", "changed": True}
    assert make["independent"] is False
    assert make["identifier_binding"] == "caller"


def test_invalid_batch_row_keeps_position_and_valid_neighbors():
    result = identity.decode_vehicle_identities(
        [
            {"identifier": synthetic_identifier(), "make": "BMW", "model": "3 Series", "model_year": "2018"},
            {"identifier": {"sensitive": "rejected"}},
            {"identifier": synthetic_identifier(), "make": "BMW", "model": "5 Series"},
        ],
        live_vpic=False,
    )
    assert result["count"] == 3
    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert [row["item_index"] for row in result["results"]] == [0, 1, 2]
    assert result["results"][1]["status"] == "invalid_input"
    assert result["results"][0]["normalization_notes"] == [{"code": "year_string_normalized", "field": "model_year"}]
    assert result["results"][2]["vehicle_profile"]["model"] == "5 Series"
    assert "rejected" not in json.dumps(result)


def test_oversized_batch_refuses_before_collection(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Oversized batch must not schedule provider work")

    monkeypatch.setattr("autostop_manager.vehicle_identity_transport.collect_identity_provider_results", blocked)
    result = identity.decode_vehicle_identities([{}] * 501)
    assert result["status"] == "invalid_input"
    assert result["count"] == 0
    assert result["errors"][0]["code"] == "identity_batch_too_large"


def test_legacy_conflicts_keep_exact_guard_without_stopping_family_research():
    result = {
        "ok": True,
        "vehicle_profile": {"make": "BMW", "model": "3 Series"},
        "conflicts": [{"severity": "medium", "field": "model_year"}],
    }
    assert identity_has_blocking_conflicts(result, "vehicle")
    assert identity_allows_lookup(result, "family")


def test_unknown_policy_scope_is_not_silently_treated_as_family():
    with pytest.raises(ValueError, match="unsupported_identity_scope"):
        identity_allows_lookup({}, "typo")
