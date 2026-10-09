"""Nonnetwork native-catalog evidence and conservative fitment regression tests."""

from __future__ import annotations

import socket
from copy import deepcopy

import pytest

from autostop_manager import automotive_parts as parts
from autostop_manager.automotive_contracts import (
    PUBLIC_OEM_CATALOG_NAMESPACES,
    catalog_ref,
    oem_catalog_ref,
    public_oem_catalog_ref,
    same_oem_catalog_ref,
    validate_catalog_context,
)

BINDING = {"version": 1, "identifier_kind": "vin", "identifier_sha256": "a" * 64}
PART = {"number": "DEMO100", "brand": "DEMO"}
TECDOC_REF = {
    "provider": "partsapi_ru",
    "namespace": "tecdoc",
    "entity_kind": "modification",
    "id": "42",
    "carType": "PC",
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("evidence contracts must not acquire hidden network data")

    monkeypatch.setattr(socket, "socket", denied)


def _ref(provider="elcats_catalog", **changes):
    return {
        "provider": provider,
        "namespace": PUBLIC_OEM_CATALOG_NAMESPACES[provider],
        "entity_kind": "modification",
        "id": "demo-vehicle",
        "entry_id": "demo-passenger-entry",
        "vehicle_context": {"make": "DEMO", "model": "MODEL1", "engine": "E1"},
        **changes,
    }


def _vehicle(ref, **profile):
    return {"vehicle_profile": {**ref["vehicle_context"], **profile}, "catalog_ref": ref, "input_binding": BINDING}


def _primary(ref, **changes):
    return {
        "provider": "demo_manufacturer",
        "primary_lineage": "DEMO independently supplied OEM document",
        "method": "supplied_document",
        "locator": "https://example.com/demo-oem-document",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "declares_oem": True,
        "fitment_assertion": True,
        "part_number": "DEMO100",
        "brand": "DEMO",
        "scope": "modification",
        "catalog_ref": ref,
        "conditions": {},
        **changes,
    }


@pytest.mark.parametrize("provider", PUBLIC_OEM_CATALOG_NAMESPACES)
def test_native_refs_keep_provider_namespace_and_independent_primary_evidence(provider):
    ref = _ref(provider)
    assert public_oem_catalog_ref(ref)
    assert oem_catalog_ref(ref)
    assert not catalog_ref(ref, namespace="tecdoc", entity_kind="modification")
    source = _primary(ref)
    capture = parts.capture_oem_evidence("DEMO100", source, "modification", ref, "DEMO")
    assert capture["data"]["candidate"]["oem_confirmed"] is True
    assert capture["data"]["candidate"]["fitment_confirmed"] is False
    assessed = parts.assess_part_fitment(_vehicle(ref), PART, {}, [source])
    assert assessed["data"]["state"] == "supported"
    assert capture["execution"]["network_calls"] == assessed["execution"]["network_calls"] == 0


@pytest.mark.parametrize("provider", PUBLIC_OEM_CATALOG_NAMESPACES)
@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_public_catalog_cannot_promote_itself_with_primary_or_verified_flags(provider, scope):
    ref = _ref(provider)
    source = _primary(ref, provider=provider, scope=scope, identifier_binding=BINDING, identifier_verified=True)
    binding = BINDING if scope == "exact_identifier" else ref
    capture = parts.capture_oem_evidence("DEMO100", source, scope, binding, "DEMO")
    assert capture["data"]["candidate"]["oem_confirmed"] is False
    assert capture["data"]["candidate"]["kind"] == "unknown"
    assessed = parts.assess_part_fitment(_vehicle(ref), PART, {}, [source], scope)
    assert assessed["data"]["state"] == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "partsapi_ru"},
        {"namespace": "tecdoc"},
        {"namespace": "japancats_epc"},
        {"id": 42},
        {"id": True},
        {"id": ""},
        {"entry_id": ""},
        {"entry_id": False},
        {"vehicle_context": {}},
        {"vehicle_context": {"make": "DEMO", "model": "MODEL1", "vin": "redacted"}},
        {"parent_ref": {"id": "unexpected-parent"}},
    ],
)
def test_native_ref_rejects_namespace_relabeling_and_missing_vehicle_context(change):
    assert not public_oem_catalog_ref({**_ref(), **change})


def test_transport_fields_do_not_change_semantic_identity():
    ref = _ref(path="/demo/catalog", parameters={"model": "demo"})
    routed = {**ref, "path": "/demo/another-view", "parameters": {"model": "demo", "lang": "en"}}
    assert same_oem_catalog_ref(ref, routed)
    source = _primary(routed)
    assert parts.capture_oem_evidence("DEMO100", source, "modification", ref, "DEMO")["data"]["candidate"][
        "oem_confirmed"
    ]
    assert parts.assess_part_fitment(_vehicle(ref), PART, {}, [source])["data"]["state"] == "supported"
    assert not same_oem_catalog_ref(ref, {**routed, "entry_id": "other-brand-entry"})


def test_parent_chain_cannot_cross_provider_entry_vehicle_or_entity_kind():
    modification = _ref()
    group = _ref(entity_kind="group", id="brakes", parent_ref=modification)
    diagram = _ref(entity_kind="diagram", id="front-brakes", parent_ref=group)
    part = _ref(entity_kind="part", id="position-1", parent_ref=diagram)
    assert public_oem_catalog_ref(part, entity_kind="part")
    for changed_parent in (
        _ref("japancats_catalog", entity_kind="group", id="brakes", parent_ref=_ref("japancats_catalog")),
        {**group, "entry_id": "another-entry"},
        {**group, "vehicle_context": {"make": "OTHER", "model": "MODEL2"}},
        modification,
    ):
        assert not public_oem_catalog_ref({**diagram, "parent_ref": changed_parent}, entity_kind="diagram")
    group["parent_ref"] = group
    assert not public_oem_catalog_ref(group, entity_kind="group")


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
@pytest.mark.parametrize("change", [{"make": "OTHER"}, {"model": "MODEL2"}, {"engine": "E2"}])
def test_native_context_cannot_escape_vehicle_comparison_through_scope(scope, change):
    ref = _ref()
    source = _primary(ref, scope=scope, identifier_binding=BINDING, identifier_verified=True)
    assert not oem_catalog_ref(ref, vehicle_profile={**ref["vehicle_context"], **change})
    result = parts.assess_part_fitment(_vehicle(ref, **change), PART, {}, [source], scope)
    assert result["data"]["state"] == "unknown"
    assert "vehicle_catalog_context" in result["missing_fields"]


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_catalog_engine_context_cannot_supply_a_missing_vehicle_fact(scope):
    ref = _ref()
    vehicle = _vehicle(ref)
    vehicle["vehicle_profile"].pop("engine")
    source = _primary(ref, scope=scope, identifier_binding=BINDING, identifier_verified=True)
    result = parts.assess_part_fitment(vehicle, PART, {}, [source], scope)
    assert result["data"]["state"] == "unknown"
    assert "vehicle_catalog_context" in result["missing_fields"]


def test_native_refs_do_not_enter_partsapi_group_and_directory_operations():
    ref = _ref()
    assert parts.resolve_catalog_group({"rows": []}, "передние колодки", ref)["outcome"] == "invalid_input"
    errors = validate_catalog_context(
        "getCars",
        {"carType": "PC", "makeId": 7, "modelId": 8},
        {"make": _ref(entity_kind="make", id="7"), "model": _ref(entity_kind="model", id="8")},
    )
    assert errors
    assert oem_catalog_ref(TECDOC_REF)
    assert not oem_catalog_ref({**TECDOC_REF, "id": True})
    primary = _primary(TECDOC_REF)
    assert parts.capture_oem_evidence("DEMO100", primary, "modification", TECDOC_REF, "DEMO")["data"]["candidate"][
        "oem_confirmed"
    ]
    assert parts.assess_part_fitment({"catalog_ref": TECDOC_REF}, PART, {}, [primary])["data"]["state"] == "supported"


@pytest.mark.parametrize("verified", [None, False, 0, 1, "true"])
def test_ready_identifier_digest_does_not_verify_native_catalog_exact_fitment(verified):
    ref = _ref()
    source = _primary(ref, scope="exact_identifier", identifier_binding=BINDING, identifier_verified=verified)
    assert not parts.capture_oem_evidence("DEMO100", source, "exact_identifier", BINDING, "DEMO")["data"]["candidate"][
        "oem_confirmed"
    ]
    result = parts.assess_part_fitment(_vehicle(ref), PART, {}, [source], "exact_identifier")
    assert result["data"]["state"] == "unknown"


def test_independent_primary_exact_identifier_remains_possible_with_explicit_verification():
    ref = _ref()
    source = _primary(ref, scope="exact_identifier", identifier_binding=BINDING, identifier_verified=True)
    assert parts.capture_oem_evidence("DEMO100", source, "exact_identifier", BINDING, "DEMO")["data"]["candidate"][
        "oem_confirmed"
    ]
    assert (
        parts.assess_part_fitment(_vehicle(ref), PART, {}, [source], "exact_identifier")["data"]["state"] == "supported"
    )
    source.pop("catalog_ref")
    assert (
        parts.assess_part_fitment(_vehicle(ref), PART, {}, [source], "exact_identifier")["data"]["state"] == "supported"
    )


@pytest.mark.parametrize("field", ["unparsed_conditions", "unparsed_restrictions", "inherited_restrictions"])
@pytest.mark.parametrize("restriction", [["unknown source condition"], "unparsed text", None])
@pytest.mark.parametrize("location", ["part", "source"])
def test_unresolved_or_malformed_restrictions_never_become_supported(field, restriction, location):
    ref = _ref()
    source, part = _primary(ref), dict(PART)
    (source if location == "source" else part)[field] = restriction
    result = parts.assess_part_fitment(_vehicle(ref), part, {}, [source])
    assert result["data"]["state"] == "unknown"


@pytest.mark.parametrize("location", ["vehicle", "profile"])
def test_unparsed_vehicle_conditions_are_not_dropped(location):
    ref = _ref()
    vehicle = _vehicle(ref)
    (vehicle if location == "vehicle" else vehicle["vehicle_profile"])["unparsed_conditions"] = ["unknown option"]
    assert parts.assess_part_fitment(vehicle, PART, {}, [_primary(ref)])["data"]["state"] == "unknown"


def test_public_unparsed_and_parsed_restrictions_survive_independent_primary_evidence():
    ref = _ref()
    primary = _primary(ref)
    public = _primary(ref, provider="elcats_catalog", document_kind="public_catalog_page", fitment_assertion=False)
    public["unparsed_conditions"] = ["condition not interpreted"]
    assert parts.assess_part_fitment(_vehicle(ref), PART, {}, [primary, public])["data"]["state"] == "unknown"
    public["unparsed_conditions"] = []
    public["conditions"] = {"pr_codes": {"none_of": ["1ZE"]}}
    assert (
        parts.assess_part_fitment(_vehicle(ref, pr_codes=["1ZP"]), PART, {}, [primary, public])["data"]["state"]
        == "unknown"
    )
    assert (
        parts.assess_part_fitment(_vehicle(ref, pr_codes=["1ZE"]), PART, {}, [primary, public])["data"]["state"]
        == "rejected"
    )
    public.update(part_number="UNRELATED", unparsed_conditions=["unrelated part condition"])
    assert parts.assess_part_fitment(_vehicle(ref), PART, {}, [primary, public])["data"]["state"] == "supported"


def test_unbound_inherited_condition_is_not_hidden_by_separate_primary_evidence():
    ref = _ref()
    primary = _primary(ref)
    public = _primary(ref, provider="elcats_catalog", document_kind="public_catalog_page", fitment_assertion=False)
    inherited = [{"conditions": {"pr_codes": {"all_of": ["1ZE"]}}, "binding": "diagram"}]
    public["inherited_conditions"] = inherited
    vehicle = _vehicle(ref, pr_codes=["1ZE"], pr_codes_complete=True)
    result = parts.assess_part_fitment(vehicle, PART, {}, [primary, public])
    assert result["data"]["state"] == "unknown"
    assert "evidence[1].inherited_conditions" in result["missing_fields"]
    public["inherited_conditions"] = []
    candidate = {**PART, "inherited_conditions": inherited}
    assert parts.assess_part_fitment(vehicle, candidate, {}, [primary])["data"]["state"] == "unknown"
    assert parts.assess_part_fitment(vehicle, PART, {}, [primary])["data"]["state"] == "supported"


@pytest.mark.parametrize(
    ("expected", "observed", "complete", "state"),
    [
        ({"all_of": ["1ZE"]}, ["1ZE", "1ZP"], False, "supported"),
        ({"all_of": ["1ZE", "1ZP"]}, ["1ZE"], False, "unknown"),
        ({"all_of": ["1ZE", "1ZP"]}, ["1ZE"], True, "rejected"),
        ({"any_of": ["1ZE", "1ZP"]}, ["1ZP"], False, "supported"),
        ({"any_of": ["1ZE", "1ZP"]}, [], False, "unknown"),
        ({"any_of": ["1ZE", "1ZP"]}, [], True, "rejected"),
        ({"none_of": ["1ZE"]}, ["1ZE"], False, "rejected"),
        ({"none_of": ["1ZE"]}, ["1ZP"], False, "unknown"),
        ({"none_of": ["1ZE"]}, ["1ZP"], True, "supported"),
        ({"none_of": ["1ZE"]}, [], True, "supported"),
        ({"none_of": ["1ZE"]}, None, True, "unknown"),
        ({"none_of": ["1ZE"]}, [], 1, "unknown"),
        ({"none_of": ["1ZE"]}, [], "true", "unknown"),
        ({"all_of": ["1ze"], "any_of": ["1ZP"], "none_of": ["1ZF"]}, ["1ZE", "1ZP"], False, "unknown"),
        ({"all_of": ["1ZE"], "any_of": ["1ZP"], "none_of": ["1ZF"]}, ["1ZE", "1ZP"], True, "supported"),
        ({"all_of": ["1ZE"], "none_of": ["1ZP"]}, ["1ZP"], False, "rejected"),
        ({"all_of": []}, [], True, "unknown"),
        ({"any_of": "1ZE"}, ["1ZE"], True, "unknown"),
        ({"all_of": [True]}, ["1ZE"], True, "unknown"),
        ({"all_of": ["BAD-CODE"]}, ["1ZE"], True, "unknown"),
        ({"all_of": ["1ZE"], "unsupported_operator": ["1ZP"]}, ["1ZE"], True, "unknown"),
        ({"all_of": ["1ZE"]}, "1ZE", True, "unknown"),
        ({"all_of": ["1ZE"]}, ["1ZE", None], True, "unknown"),
    ],
)
@pytest.mark.parametrize("location", ["criteria", "source"])
def test_pr_codes_use_three_valued_logic_and_explicit_completeness(expected, observed, complete, state, location):
    ref, source = _ref(), _primary(_ref())
    criteria = {}
    (criteria if location == "criteria" else source["conditions"])["pr_codes"] = expected
    vehicle = _vehicle(ref, pr_codes=observed, pr_codes_complete=complete)
    before = deepcopy((vehicle, criteria, source))
    result = parts.assess_part_fitment(vehicle, PART, criteria, [source])
    assert result["data"]["state"] == state
    assert (vehicle, criteria, source) == before
    assert result["execution"]["network_calls"] == 0


def test_pr_completeness_defaults_false_and_source_conditions_are_not_overridden():
    ref = _ref()
    source = _primary(ref, conditions={"pr_codes": {"all_of": ["1ZP"]}})
    criteria = {"pr_codes": {"any_of": ["1ZE"]}}
    assert (
        parts.assess_part_fitment(_vehicle(ref, pr_codes=["1ZE"]), PART, criteria, [source])["data"]["state"]
        == "unknown"
    )
    assert (
        parts.assess_part_fitment(_vehicle(ref, pr_codes=["1ZE"], pr_codes_complete=True), PART, criteria, [source])[
            "data"
        ]["state"]
        == "rejected"
    )


def test_candidate_number_scope_and_outer_catalog_context_are_preserved():
    ref = _ref()
    source = _primary(ref)
    candidate = {"normalized_number": "DEMO100", "brand": "DEMO", "scope": "modification", "unparsed_conditions": []}
    envelope = {"data": {"vehicle_profile": ref["vehicle_context"]}, "catalog_ref": ref}
    before = deepcopy((envelope, candidate, source))
    assert parts.assess_part_fitment(envelope, candidate, {}, [source])["data"]["state"] == "supported"
    assert (envelope, candidate, source) == before
    assert (
        parts.assess_part_fitment(envelope, {**candidate, "scope": "family"}, {}, [source])["data"]["state"]
        == "unknown"
    )
    envelope["data"]["catalog_ref"] = _ref(id="another-vehicle")
    result = parts.assess_part_fitment(envelope, candidate, {}, [source])
    assert result["data"]["state"] == "conflict"
    assert result["data"]["checked_conditions"] == []
