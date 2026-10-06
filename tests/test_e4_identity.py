from __future__ import annotations

from copy import deepcopy
import json
import subprocess
import sys

import pytest

from autostop_manager import e4_identity as e4
from autostop_manager import vehicle_identity as legacy


def synthetic_vin(prefix="WBA"):
    return prefix + "0" * (17 - len(prefix))


def clean_observation(vin=None, **fields):
    vin = vin or synthetic_vin()
    result = {
        "ok": True,
        "outcome": "success",
        "error_code": "0",
        "vin": vin,
        "vehicle": {"vin": vin, "make": "BMW", "model": "3 Series", **fields},
    }
    return e4.vpic_observation(vin, result)


def forbid_sources(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unselected decoder or network was invoked")

    monkeypatch.setattr(e4.vin_lookup, "decode_vin_vpic", forbidden)
    monkeypatch.setattr(e4.vin_lookup, "decode_wmi_vpic", forbidden)
    monkeypatch.setattr(e4.vin_lookup, "urlopen", forbidden)
    monkeypatch.setattr(legacy, "decode_vehicle_identity", forbidden)


def test_local_sources_and_reconcile_do_not_dispatch_other_sources(monkeypatch):
    forbid_sources(monkeypatch)
    vin = synthetic_vin("WVWZZZAU")
    inspected = e4.inspect_vehicle_identifier(vin)
    wmi = e4.decode_wmi_local("WVW")
    brand = e4.vin_brand_details(vin)
    result = e4.reconcile_vehicle_identity(vin, [wmi, brand])
    assert inspected["ok"]
    assert result["vehicle_profile"]["model_family"] == "Golf"
    assert result["parts_lookup_readiness"]["ready_for_family_lookup"]
    assert not result["parts_lookup_readiness"]["ready_for_modification_lookup"]
    assert result["processing"]["network_calls"] == 0
    assert vin not in json.dumps(result)


def test_legacy_builder_remains_pure_and_old_contract_unchanged(monkeypatch):
    forbid_sources(monkeypatch)
    checked = e4.validate_identity_input(synthetic_vin("WVWZZZAU"))
    local = legacy.collect_legacy_local_sources(checked["identifier"])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure builder queried local source")

    monkeypatch.setattr(legacy, "_merge_local_wmi_hint", forbidden)
    monkeypatch.setattr(legacy, "_merge_platform_rule", forbidden)
    monkeypatch.setattr(legacy, "_matching_platform_rule", forbidden)
    result = legacy.build_legacy_identity_dossier(checked, None, None, local_sources=local)
    assert result["schema_version"] == 2
    assert result["vehicle_profile"]["model_family"] == "Golf"


def test_reconcile_does_not_add_local_hints_with_empty_sources(monkeypatch):
    forbid_sources(monkeypatch)
    result = e4.reconcile_vehicle_identity(synthetic_vin("WVWZZZAU"), [])
    assert result["vehicle_profile"] == {}
    assert not result["parts_lookup_readiness"]["ready_for_family_lookup"]


def test_failed_source_does_not_hide_successful_source():
    vin = synthetic_vin("WVWZZZAU")
    failed = e4.make_observation("corgi_decode", vin, outcome="database_missing")
    result = e4.reconcile_vehicle_identity(vin, [failed, e4.vin_brand_details(vin)])
    assert result["status"] == "partial"
    assert result["vehicle_profile"]["model_family"] == "Golf"
    assert result["provider_errors"][0]["code"] == "database_missing"


def test_binding_rejects_other_vin_even_when_masked_displays_collide():
    vin = synthetic_vin()
    foreign = vin[:8] + "1" + vin[9:]
    assert e4.vin_lookup._redact_identifier(vin) == e4.vin_lookup._redact_identifier(foreign)
    result = e4.reconcile_vehicle_identity(vin, [clean_observation(foreign)])
    assert not result["ok"]
    assert result["errors"][0]["code"] == "identity_observation_input_mismatch"


@pytest.mark.parametrize("replacement", [None, "bad", [], {"scope": "wrong"}])
def test_malformed_binding_is_safe_error(replacement):
    observation = clean_observation()
    observation["input_binding"] = replacement
    result = e4.reconcile_vehicle_identity(synthetic_vin(), [observation])
    assert not result["ok"]
    assert result["errors"][0]["code"] == "invalid_identity_observation"


def test_untrusted_readiness_and_strength_do_not_promote_local_evidence():
    vin = synthetic_vin("WVWZZZAU")
    observation = e4.vin_brand_details(vin)
    observation["parts_lookup_readiness"] = {"ready_for_crm_writeback": True, "ready_for_vehicle_lookup": True}
    observation["confidence"] = 1.0
    observation["diagnostics"].update(provider_clean=True, identifier_verified=True)
    for row in observation["field_evidence"]:
        row["strength"] = "supported"
    result = e4.reconcile_vehicle_identity(vin, [observation])
    assert result["confidence_label"] == "medium"
    assert all(row["strength"] == "candidate" for row in result["field_evidence"])
    assert not result["parts_lookup_readiness"]["ready_for_crm_writeback"]
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


def test_nhtsa_duplicates_do_not_increase_evidence_or_score():
    vin = synthetic_vin()
    online = clean_observation(vin, enginemodel="N20")
    corgi = e4.make_observation(
        "corgi_decode",
        vin,
        fields={"make": "BMW", "model": "3 Series", "engine": "N20"},
        diagnostics={"provider_clean": True, "identifier_verified": True},
    )
    one = e4.reconcile_vehicle_identity(vin, [online])
    many = e4.reconcile_vehicle_identity(vin, [online, deepcopy(online), corgi])
    assert many["confidence"] == one["confidence"]
    assert len(many["field_evidence"]) == len(one["field_evidence"])
    assert many["processing"]["independent_origin_count"] == 1


def test_same_origin_disagreement_is_preserved():
    vin = synthetic_vin()
    online = clean_observation(vin, enginemodel="N20")
    corgi = e4.make_observation(
        "corgi_decode",
        vin,
        fields={"make": "BMW", "model": "3 Series", "engine": "B48"},
        diagnostics={"provider_clean": True, "identifier_verified": True},
    )
    result = e4.reconcile_vehicle_identity(vin, [online, corgi])
    assert result["field_statuses"]["engine"]["status"] == "disputed"
    assert "engine" not in result["vehicle_profile"]
    assert len(result["field_statuses"]["engine"]["alternatives"]) == 2
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


def test_hint_year_is_bound_and_not_independent():
    vin = synthetic_vin()
    observation = e4.make_observation(
        "corgi_decode",
        vin,
        fields={"make": "BMW", "model": "3 Series", "model_year": 2018},
        model_year=2018,
        diagnostics={"provider_clean": True, "identifier_verified": True},
    )
    result = e4.reconcile_vehicle_identity(vin, [observation], crm_context={"model_year": 2018})
    rows = [row for row in result["field_evidence"] if row["field"] == "model_year"]
    assert all(row["strength"] == "candidate" for row in rows)
    assert any(row["depends_on"] == ["caller.model_year"] for row in rows)
    mismatch = e4.reconcile_vehicle_identity(vin, [observation], crm_context={"model_year": 2019})
    assert mismatch["errors"][0]["code"] == "identity_observation_hint_mismatch"


def test_provider_echo_is_verified_before_redaction():
    vin = synthetic_vin()
    other = synthetic_vin("WVW")
    observation = e4.vpic_observation(
        vin,
        {
            "ok": True,
            "outcome": "success",
            "error_code": "0",
            "vin": other,
            "vehicle": {"make": "WRONG", "model": "WRONG"},
        },
    )
    assert observation["outcome"] == "identity_mismatch"
    assert not observation["vehicle_profile"]
    assert other not in json.dumps(observation)
    assert vin not in json.dumps(observation)


def test_nonclean_vpic_drops_variant_fields():
    vin = synthetic_vin()
    observation = e4.vpic_observation(
        vin,
        {
            "ok": True,
            "outcome": "success",
            "error_code": "1",
            "vin": vin,
            "vehicle": {"make": "BMW", "model": "3 Series", "enginemodel": "WRONG", "transmissionstyle": "WRONG"},
        },
    )
    result = e4.reconcile_vehicle_identity(vin, [observation])
    assert "engine" not in result["vehicle_profile"]
    assert "transmission" not in result["vehicle_profile"]
    assert result["field_statuses"]["model"]["status"] == "candidate"


def test_corgi_candidate_duplicate_cannot_hide_stronger_online_result():
    vin = synthetic_vin()
    candidate = e4.make_observation("corgi_decode", vin, fields={"make": "BMW", "model": "3 Series"})
    result = e4.reconcile_vehicle_identity(vin, [candidate, clean_observation(vin)])
    assert result["field_statuses"]["model"]["status"] == "supported"


def test_quomation_family_rules_have_provenance_and_unknown_engine():
    vin = synthetic_vin("WAUZZZ8K")
    observation = e4.vin_brand_details(vin)
    assert observation["vehicle_profile"]["model_family"] == "A4 B8"
    assert "make" not in observation["missing_fields"]
    result = e4.reconcile_vehicle_identity(vin, [observation])
    assert observation["diagnostics"]["rules"][0]["license"] == "Apache-2.0"
    assert result["vehicle_profile"]["model_family"] == "A4 B8"
    assert {row["data_origin_id"] for row in result["field_evidence"]} == {"quomation_vin_rules"}
    assert "engine" not in result["vehicle_profile"]


def test_frame_rules_separate_and_keep_only_family():
    frame = "ES1-" + "0" * 7
    result = e4.reconcile_vehicle_identity(frame, [e4.decode_frame_local(frame)])
    assert result["vehicle_profile"]["model"] == "Civic"
    assert result["identifier_validation"]["valid_for_vehicle_lookup"]
    assert not result["parts_lookup_readiness"]["ready_for_modification_lookup"]
    assert e4.vin_brand_details(frame)["outcome"] == "unsupported"
    assert e4.decode_frame_local(synthetic_vin())["outcome"] == "unsupported"


def test_six_character_wmi_and_prefix_are_bound_only_to_compatible_vin():
    vin = synthetic_vin("1G9")
    wmi = e4.wmi_for_vin(vin)
    observation = e4.decode_wmi_local(wmi)
    assert observation["input_binding"]["scope"] == "wmi"
    assert e4.reconcile_vehicle_identity(vin, [observation])["ok"]
    assert not e4.reconcile_vehicle_identity(synthetic_vin("WBA"), [observation])["ok"]
    assert e4.decode_wmi_local(vin)["outcome"] == "invalid_input"


def test_european_checksum_is_caveat_without_implied_wrong_vin():
    vin = synthetic_vin("WVWZZZAU")
    result = e4.reconcile_vehicle_identity(vin, [e4.vin_brand_details(vin)])
    assert result["diagnostics"]["check_digit"]["status"] == "fail"
    assert result["identifier_validation"]["valid_for_vehicle_lookup"]


@pytest.mark.parametrize("observations", [None, {}, [None], [{"schema_version": 2}], [] * 101])
def test_observation_shape_validation(observations):
    if observations == []:
        observations = [{}] * 101
    result = e4.reconcile_vehicle_identity(synthetic_vin(), observations)
    assert not result["ok"]


def test_explicit_partial_has_partial_binding_and_no_exact_fields():
    partial = "WAUZZZ8K"
    observation = e4.vin_brand_details(partial, identifier_type="vin_partial")
    assert observation["input_binding"]["scope"] == "partial"
    result = e4.reconcile_vehicle_identity(partial, [observation], identifier_type="vin_partial")
    assert result["parts_lookup_readiness"]["ready_for_family_lookup"]
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool", []),
        ("input_binding", {"scope": []}),
        ("schema_version", True),
        ("field_evidence", [{"field": [], "value": "bad"}]),
    ],
)
def test_unhashable_and_boolean_schema_shapes_are_rejected(field, value):
    observation = clean_observation()
    observation[field] = value
    assert not e4.reconcile_vehicle_identity(synthetic_vin(), [observation])["ok"]


def test_vin_year_and_build_date_are_separate_facts():
    vin = synthetic_vin()
    observation = clean_observation(vin, modelyear=2018)
    result = e4.reconcile_vehicle_identity(
        vin, [observation], crm_context={"production_year": 2017, "production_date": "2017-11-30"}
    )
    assert result["field_statuses"]["model_year"]["status"] == "supported"
    assert not result["conflicts"]


def test_na_checksum_blocks_exact_lookup_only_with_na_market_context():
    vin = synthetic_vin()
    result = e4.reconcile_vehicle_identity(vin, [clean_observation(vin)], crm_context={"market": "North America"})
    assert result["diagnostics"]["check_digit"]["status"] == "fail"
    assert not result["identifier_validation"]["valid_for_vehicle_lookup"]
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


def test_actual_local_tools_do_not_import_catalog_or_server_infrastructure():
    code = """
import sys
from autostop_manager.e4_identity import inspect_vehicle_identifier, decode_wmi_local, vin_brand_details, decode_frame_local
inspect_vehicle_identifier('WVWZZZAU' + '0' * 9)
decode_wmi_local('WVW')
vin_brand_details('WVWZZZAU' + '0' * 9)
decode_frame_local('ES1-' + '0' * 7)
blocked = ('autostop_manager.catalog_clients', 'autostop_manager.storage', 'autostop_manager.mcp_server', 'autostop_manager.catalog_adapters')
assert not any(module in sys.modules for module in blocked)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_eu_market_alias_and_legacy_rule_validation_are_explicit():
    observation = e4.vin_brand_details(synthetic_vin("WAUZZZ8K"), market="EU")
    assert observation["ok"]
    assert observation["diagnostics"]["rules"][0]["source_commit"]
    old = e4.vin_brand_details(synthetic_vin("WVWZZZAU"))
    assert old["diagnostics"]["rules"][0]["validation"] == "legacy_unverified"


def test_vpic_vehicle_descriptor_does_not_discard_valid_partial_fields():
    partial = "5UXWX7C5*BA"
    raw = {
        "ok": True,
        "outcome": "success",
        "error_code": "0",
        "vin": partial,
        "vehicle": {
            "vin": partial,
            "make": "BMW",
            "model": "X3",
            "modelyear": "2011",
            "vehicledescriptor": "5UXWX7C5*BA",
            "enginemodel": "N52",
            "vehicletype": "MULTIPURPOSE PASSENGER VEHICLE (MPV)",
        },
    }
    observation = e4.vpic_observation(partial, raw)
    assert observation["outcome"] == "success"
    assert observation["vehicle_profile"]["make"] == "BMW"
    assert observation["vehicle_profile"]["model"] == "X3"
    assert "vehicle_descriptor" not in observation["vehicle_profile"]
    assert "vehicle_descriptor" in observation["diagnostics"]
    result = e4.reconcile_vehicle_identity(partial, [observation])
    assert result["vehicle_profile"]["model"] == "X3"
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


@pytest.mark.parametrize("codes", [None, ["100"], "", {}])
def test_corgi_contradictory_or_missing_error_metadata_cannot_be_supported(codes):
    vin = synthetic_vin()
    observation = e4.make_observation(
        "corgi_decode",
        vin,
        fields={"make": "BMW", "model": "3 Series", "engine": "N20"},
        diagnostics={"provider_clean": True, "identifier_verified": True, "provider_error_codes": codes},
    )
    result = e4.reconcile_vehicle_identity(vin, [observation])
    assert all(row["strength"] == "candidate" for row in result["field_evidence"])
    assert not result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


def test_corgi_empty_errors_and_success_support_available_bound_fields():
    vin = synthetic_vin()
    observation = e4.make_observation(
        "corgi_decode",
        vin,
        fields={"make": "BMW", "model": "3 Series"},
        diagnostics={"provider_clean": True, "identifier_verified": True, "provider_error_codes": []},
    )
    result = e4.reconcile_vehicle_identity(vin, [observation])
    assert result["field_statuses"]["model"]["status"] == "supported"
    assert result["parts_lookup_readiness"]["ready_for_vehicle_lookup"]


def test_stronger_vin_witness_replaces_wmi_provenance_without_duplicate_evidence():
    vin = synthetic_vin()
    wmi = e4.make_observation("decode_wmi_vpic", "WBA", fields={"make": "BMW"}, version="wmi-old")
    online = clean_observation(vin)
    online["source"]["version"] = "vin-current"
    forward = e4.reconcile_vehicle_identity(vin, [wmi, online])
    reverse = e4.reconcile_vehicle_identity(vin, [online, wmi])
    assert len(forward["field_evidence"]) == len(reverse["field_evidence"]) == 2
    assert forward["parts_lookup_readiness"] == reverse["parts_lookup_readiness"]
    assert forward["parts_lookup_readiness"]["ready_for_vehicle_lookup"]
    for result in (forward, reverse):
        row = next(row for row in result["field_evidence"] if row["field"] == "make")
        assert row["source"] == "decode_vin_vpic"
        assert row["source_kind"] == "provider"
        assert row["source_version"] == "vin-current"
        assert row["bound"] is True
        assert row["identifier_binding"] == "exact"
        assert row["strength"] == "supported"
        assert result["provenance"]["make"][0]["source"] == "decode_vin_vpic"


def test_equal_supported_duplicate_keeps_original_witness_metadata():
    vin = synthetic_vin()
    original = clean_observation(vin)
    original["source"]["version"] = "original"
    duplicate = clean_observation(vin)
    duplicate["source"]["version"] = "later"
    result = e4.reconcile_vehicle_identity(vin, [original, duplicate])
    assert all(row["source_version"] == "original" for row in result["field_evidence"])
