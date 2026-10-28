from __future__ import annotations

from copy import deepcopy
import socket
import subprocess

import pytest


from autostop_manager import automotive_contracts as contracts
from autostop_manager import automotive_identity as identity
from autostop_manager import automotive_parts as parts
from autostop_manager import vehicle_identity_policy as policy
from autostop_manager import vin_lookup


@pytest.fixture(autouse=True)
def synthetic_dependencies_only(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Forbidden provider/cache call")

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "synthetic.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "no-runtime"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setattr(vin_lookup, "decode_vin_vpic", denied)
    monkeypatch.setattr(vin_lookup, "decode_wmi_vpic", denied)
    monkeypatch.setattr(vin_lookup, "urlopen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)


def _reference(identifier="42"):
    return {
        "provider": "partsapi_ru",
        "namespace": "tecdoc",
        "entity_kind": "modification",
        "id": identifier,
        "carType": "PC",
    }


_BAD_IDS = [pytest.param("²", id="nondecimal-digit"), pytest.param("1" * 4301, id="int-conversion-limit")]


@pytest.mark.parametrize("identifier", _BAD_IDS)
@pytest.mark.parametrize("route", ["reference", "parameters"])
def test_malformed_numeric_catalog_identifier_returns_invalid_without_exception(identifier, route):
    if route == "reference":
        assert contracts.catalog_ref(_reference(identifier), namespace="tecdoc", entity_kind="modification") is False
    else:
        assert contracts.catalog_parameter_errors({"carId": identifier}) == ["invalid_carId"]


@pytest.mark.parametrize("identifier", ["42", 42])
@pytest.mark.parametrize("route", ["reference", "parameters"])
def test_existing_numeric_catalog_identifier_remains_valid(identifier, route):
    if route == "reference":
        assert contracts.catalog_ref(_reference(identifier), namespace="tecdoc", entity_kind="modification") is True
    else:
        assert contracts.catalog_parameter_errors({"carId": identifier}) == []


def _source(reference):
    return {
        "provider": "manufacturer",
        "primary_lineage": "synthetic OEM document",
        "method": "supplied_document",
        "locator": "https://example.org/parts",
        "fetched_at": "2026-10-13T00:00:00+00:00",
        "document_kind": "official_epc",
        "scope": "modification",
        "part_number": "OEM123",
        "brand": "DEMO",
        "catalog_ref": reference,
        "declares_oem": True,
        "fitment_assertion": True,
    }


def _public_evidence_route(route, reference):
    source = _source(reference)
    if route == "capture":
        return parts.capture_oem_evidence("OEM123", source, "modification", reference, "DEMO")
    return parts.assess_part_fitment(
        {"vehicle_profile": {"make": "DEMO", "model": "MODEL"}, "catalog_ref": reference},
        {"number": "OEM123", "brand": "DEMO"},
        {},
        [source],
        "modification",
    )


@pytest.mark.parametrize("identifier", _BAD_IDS)
@pytest.mark.parametrize("route", ["capture", "fitment"])
def test_public_evidence_route_cannot_crash_or_confirm_malformed_catalog_reference(identifier, route):
    result = _public_evidence_route(route, _reference(identifier))
    assert result["execution"]["network_calls"] == 0
    if route == "capture":
        assert result["data"]["candidate"]["oem_confirmed"] is False
        assert "modification_binding" in result["missing_fields"]
    else:
        assert result["data"]["state"] == "unknown"
        assert "modification_binding" in result["missing_fields"]


@pytest.mark.parametrize("route", ["capture", "fitment"])
def test_public_evidence_route_preserves_valid_modification_reference(route):
    result = _public_evidence_route(route, _reference())
    assert result["outcome"] == "success"
    assert result["execution"]["network_calls"] == 0
    if route == "capture":
        assert result["data"]["candidate"]["oem_confirmed"] is True
        assert result["data"]["candidate"]["fitment_confirmed"] is False
    else:
        assert result["data"]["state"] == "supported"


def _edge(left, right, direction="from_to"):
    source = {
        "provider": "manufacturer",
        "primary_lineage": "synthetic document",
        "method": "supplied_document",
        "locator": "https://example.org/parts",
        "fetched_at": "2026-10-13T00:00:00+00:00",
        "document_kind": "manufacturer_supersession",
        "direction": direction,
        "from": {"number": left, "brand": "DEMO"},
        "to": {"number": right, "brand": "DEMO"},
    }
    return {
        "type": "supersession",
        "direction": direction,
        "from": source["from"],
        "to": source["to"],
        "evidence": [source],
    }


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(_edge("A123", "C123"), id="same-direction-control"),
        pytest.param(_edge("C123", "A123", "to_from"), id="reverse-direction"),
        pytest.param(_edge("A-123", "C123"), id="normalized-article"),
    ],
)
def test_supersession_branch_uses_effective_direction_and_normalized_article(second):
    rows = [_edge("A123", "B123"), deepcopy(second)]
    result = parts.compare_part_relations(rows)
    assert result["outcome"] == "partial"
    assert any(row["code"] == "supersession_branches" for row in result["conflicts"])
    assert result["execution"]["network_calls"] == 0


def test_incoming_supersession_and_outgoing_replacement_do_not_form_a_branch():
    result = parts.compare_part_relations([_edge("A123", "B123", "to_from"), _edge("A123", "C123")])
    assert result["outcome"] == "success"
    assert result["conflicts"] == []
    assert result["execution"]["network_calls"] == 0


@pytest.mark.parametrize("shape", ["structured", "flat"])
def test_disputed_field_blocks_lookup_in_both_published_status_shapes(shape):
    status = {"status": "disputed"} if shape == "structured" else "disputed"
    row = {
        "field_statuses": {"engine": status},
        "parts_lookup_readiness": {"ready_for_vehicle_lookup": True},
        "vehicle_profile": {"make": "DEMO", "model": "MODEL"},
    }
    assert contracts.identity_errors(row, row) == [{"field": "engine", "code": "disputed_ready_identity"}]
    assert policy.identity_has_blocking_conflicts(row) is True
    assert policy.identity_allows_lookup(row) is False


@pytest.mark.parametrize("shape", ["structured", "flat"])
def test_supported_field_status_shapes_compute_same_lookup_readiness_without_exception(shape):
    profile = {
        "make": "DEMO",
        "model": "MODEL",
        "model_year": 2020,
        "engine": "E1",
        "transmission": "Manual",
        "market": "EU",
    }
    statuses = {
        field: {"status": "supported"} if shape == "structured" else "supported"
        for field in ("model_year", "engine", "transmission", "market")
    }
    row = {
        "vehicle_profile": profile,
        "family_candidates": [{"profile": profile}],
        "confidence_label": "high",
        "field_statuses": statuses,
    }
    result = policy.build_parts_lookup_readiness(row)
    assert result["ready_for_vehicle_lookup"] is True
    assert result["ready_for_modification_lookup"] is True
    assert result["ready_for_crm_writeback"] is False
    assert result["exact_applicability_confirmed"] is False


@pytest.mark.parametrize("bad_enum", [[], {}], ids=["list", "object"])
@pytest.mark.parametrize(
    "route", ["relation_type", "relation_direction", "relation_document", "capture_document", "fitment_document"]
)
def test_nested_enum_shapes_remain_unverified_without_exception(bad_enum, route):
    if route.startswith("relation"):
        relation = _edge("A123", "B123")
        if route == "relation_type":
            relation["type"] = deepcopy(bad_enum)
        elif route == "relation_direction":
            relation["direction"] = deepcopy(bad_enum)
            relation["evidence"][0]["direction"] = deepcopy(bad_enum)
        else:
            relation["evidence"][0]["document_kind"] = deepcopy(bad_enum)
        response = parts.compare_part_relations([relation])
        assert response["data"]["relations"][0]["type"] == (
            "unknown" if route == "relation_type" else "unverified_supersession"
        )
        assert response["data"]["relations"][0]["fitment_confirmed"] is False
    else:
        source = _source(_reference())
        source["document_kind"] = deepcopy(bad_enum)
        if route == "capture_document":
            response = parts.capture_oem_evidence("OEM123", source, "modification", _reference(), "DEMO")
            assert response["data"]["candidate"]["oem_confirmed"] is False
            assert "primary_oem_evidence" in response["missing_fields"]
        else:
            response = parts.assess_part_fitment(
                {"vehicle_profile": {"make": "DEMO", "model": "MODEL"}, "catalog_ref": _reference()},
                {"number": "OEM123", "brand": "DEMO"},
                {},
                [source],
                "modification",
            )
            assert response["data"]["state"] == "unknown"
            assert "primary_bound_fitment_evidence" in response["missing_fields"]
    assert response["outcome"] == "partial"
    assert response["execution"]["network_calls"] == 0


_SYNTHETIC_IDENTIFIER = "1HGCM826300000001"
_BAD_ORIGIN_LISTS = [
    pytest.param(1, id="integer"),
    pytest.param(True, id="boolean"),
    pytest.param({"provider": "DEMO"}, id="object"),
    pytest.param("invalid", id="string"),
]


def _identity_row():
    return {
        "input_binding": contracts.binding(_SYNTHETIC_IDENTIFIER),
        "vehicle_profile": {"make": "DEMO", "model": "MODEL"},
    }


@pytest.mark.parametrize("evidence", _BAD_ORIGIN_LISTS)
@pytest.mark.parametrize("level", ["flat", "outer"])
@pytest.mark.parametrize("detail", ["full", "summary"])
def test_reconciliation_rejects_malformed_evidence_before_lineage_iteration(evidence, level, detail):
    row = _identity_row()
    if level == "outer":
        row = {"data": row}
    row["evidence"] = deepcopy(evidence)
    response = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [row], detail=detail)
    assert response["outcome"] == "invalid_input"
    assert response["missing_fields"] == ["results.evidence"]
    assert response["data"] == {}
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("sources", _BAD_ORIGIN_LISTS)
@pytest.mark.parametrize("level", ["flat", "outer"])
@pytest.mark.parametrize("detail", ["full", "summary"])
def test_reconciliation_malformed_legacy_sources_cannot_establish_field_lineage(sources, level, detail):
    row = _identity_row()
    if level == "outer":
        row = {"data": row}
    row["evidence_sources"] = deepcopy(sources)
    response = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [row], detail=detail)
    assert response["outcome"] == "partial"
    assert response["data"]["vehicle_profile"] == {"make": "DEMO", "model": "MODEL"}
    for field in ("make", "model"):
        assert response["data"]["provenance"][field] == [
            {
                "value": row.get("data", row)["vehicle_profile"][field],
                "primary_lineage": None,
                "independent": False,
                "result_index": 0,
            }
        ]
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("supplied", _BAD_ORIGIN_LISTS)
@pytest.mark.parametrize("key", ["evidence", "evidence_sources"])
def test_ready_identity_malformed_source_summary_stays_unverified_without_exception(supplied, key):
    row = {**_identity_row(), key: deepcopy(supplied)}
    accepted, errors = contracts.ready_identity(row, _SYNTHETIC_IDENTIFIER)
    assert accepted is not None
    assert errors == []
    assert contracts.field_origins(accepted, "make", "DEMO") == [
        {"value": "DEMO", "primary_lineage": None, "independent": False}
    ]


@pytest.mark.parametrize("key", ["evidence", "evidence_sources"])
def test_healthy_legacy_source_summary_preserves_lineage_without_independence(key):
    row = {**_identity_row(), key: [{"primary_lineage": "DEMO"}]}
    accepted, errors = contracts.ready_identity(row, _SYNTHETIC_IDENTIFIER)
    assert errors == [] and accepted is not None
    assert contracts.field_origins(accepted, "make", "DEMO") == [
        {"value": "DEMO", "primary_lineage": "DEMO", "independent": False}
    ]
    response = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [row])
    assert response["data"]["provenance"]["make"][0]["primary_lineage"] == "DEMO"
    assert response["data"]["provenance"]["make"][0]["independent"] is False
    assert response["execution"]["network_calls"] == 0


def test_reconciliation_summary_keeps_valid_nested_origins_and_reuse_without_mutating_input():
    origin = {
        "field": "make",
        "value": "DEMO",
        "primary_lineage": "DEMO",
        "strength": "candidate",
        "independent": False,
    }
    row = {
        "evidence": [origin],
        "data": {
            **_identity_row(),
            "field_evidence": [origin],
            "provenance": {"make": [origin]},
            "variants": [{"profile": {"make": "DEMO"}, "field_evidence": [origin], "provenance": {"make": [origin]}}],
        },
    }
    before = deepcopy(row)
    full = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [row])
    summary = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [row], detail="summary")
    reused = identity.reconcile_vehicle_identity(_SYNTHETIC_IDENTIFIER, [summary])
    for response in (summary, reused):
        assert response["data"]["vehicle_profile"] == full["data"]["vehicle_profile"]
        assert response["data"]["field_statuses"] == full["data"]["field_statuses"]
        for field, origins in full["data"]["provenance"].items():

            def source_facts(records):
                return {
                    (item["value"], item["primary_lineage"], item["independent"], item.get("strength"))
                    for item in records
                }

            assert source_facts(response["data"]["provenance"][field]) == source_facts(origins)
        assert response["execution"]["network_calls"] == 0
    assert summary["data"]["provenance"] == full["data"]["provenance"]
    assert row == before


def _ready_policy_row():
    profile = {
        "make": "DEMO",
        "model": "MODEL",
        "model_year": 2020,
        "engine": "E1",
        "transmission": "Manual",
        "market": "EU",
    }
    return {
        "input_binding": contracts.binding(_SYNTHETIC_IDENTIFIER),
        "vehicle_profile": profile,
        "family_candidates": [{"profile": profile}],
        "confidence_label": "high",
    }


@pytest.mark.parametrize("statuses", [["supported"], "supported", 1], ids=["list", "string", "number"])
def test_reused_identity_malformed_status_mapping_matches_existing_empty_readiness(statuses):
    row = _ready_policy_row()
    accepted, errors = contracts.ready_identity({"data": {**row, "field_statuses": statuses}}, _SYNTHETIC_IDENTIFIER)
    assert errors == [] and accepted is not None
    empty, errors = contracts.ready_identity({"data": {**row, "field_statuses": {}}}, _SYNTHETIC_IDENTIFIER)
    assert errors == [] and empty is not None
    expected = policy.build_parts_lookup_readiness(empty)
    assert policy.identity_allows_lookup(accepted) == policy.identity_allows_lookup(empty)
    assert policy.build_parts_lookup_readiness(accepted) == expected
    assert expected["ready_for_family_lookup"] is True
    assert expected["ready_for_vehicle_lookup"] is False
    assert expected["ready_for_modification_lookup"] is False
    assert expected["ready_for_crm_writeback"] is False
    assert expected["exact_applicability_confirmed"] is False


@pytest.mark.parametrize("shape", ["flat", "structured"])
def test_reused_identity_supported_status_shapes_remain_unverified_candidates(shape):
    statuses = {
        field: "supported" if shape == "flat" else {"status": "supported"}
        for field in ("model_year", "engine", "transmission", "market")
    }
    accepted, errors = contracts.ready_identity(
        {"data": {**_ready_policy_row(), "field_statuses": statuses}}, _SYNTHETIC_IDENTIFIER
    )
    assert errors == [] and accepted is not None
    assert policy.identity_allows_lookup(accepted) is False
    readiness = policy.build_parts_lookup_readiness(accepted)
    assert readiness["ready_for_family_lookup"] is True
    assert readiness["ready_for_modification_lookup"] is False
    assert accepted["trust"]["state"] == "caller_supplied_unverified"
    assert readiness["ready_for_crm_writeback"] is False
    assert readiness["exact_applicability_confirmed"] is False


@pytest.mark.parametrize(
    "requested,source_number", [("---", None), ("---", "---"), ("---", "..."), ("OEM123", "---"), ("OEM123", None)]
)
def test_empty_canonical_number_cannot_bind_primary_oem_evidence(requested, source_number):
    source = _source(_reference())
    source["scope"] = "family"
    source.pop("catalog_ref")
    source.pop("part_number")
    if source_number is not None:
        source["part_number"] = source_number
    response = parts.capture_oem_evidence(requested, source, "family", brand="DEMO")
    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["source_part_number"]
    assert response["data"]["candidate"]["kind"] == "unknown"
    assert response["data"]["candidate"]["oem_confirmed"] is False
    assert response["data"]["candidate"]["fitment_confirmed"] is False
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("requested,source_number", [("OEM-123", "OEM123"), ("OEM123", "OEM-123")])
def test_primary_oem_evidence_keeps_nonempty_raw_and_canonical_number_binding(requested, source_number):
    source = {**_source(_reference()), "scope": "family", "part_number": source_number}
    response = parts.capture_oem_evidence(requested, source, "family", brand="DEMO")
    assert response["outcome"] == "success"
    assert response["data"]["candidate"]["raw_number"] == requested
    assert response["data"]["candidate"]["normalized_number"] == "OEM123"
    assert response["data"]["candidate"]["oem_confirmed"] is True
    assert response["data"]["candidate"]["fitment_confirmed"] is False
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize("direction", ["from_to", "to_from"])
def test_empty_canonical_supersession_endpoints_cannot_create_verified_edges_or_branches(endpoint, direction):
    rows = [
        _edge(punctuation, replacement, direction) if endpoint == "from" else _edge(replacement, punctuation, direction)
        for punctuation, replacement in [("---", "OEM1"), ("...", "OEM2")]
    ]
    before = deepcopy(rows)
    response = parts.compare_part_relations(rows)
    assert response["outcome"] == "partial"
    assert response["conflicts"] == []
    assert response["missing_fields"] == [
        "relations[0].supersession_direction_and_primary_evidence",
        "relations[1].supersession_direction_and_primary_evidence",
    ]
    for row in response["data"]["relations"]:
        assert row["type"] == "unverified_supersession"
        assert row[endpoint]["normalized_number"] == ""
        assert row["fitment_confirmed"] is False
    assert response["execution"]["network_calls"] == 0
    assert rows == before
