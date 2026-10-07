"""Synthetic boundary tests for independent automotive operations."""

from __future__ import annotations

import socket
from copy import deepcopy

import pytest

from autostop_manager import automotive_identity as identity
from autostop_manager import automotive_labor as labor
from autostop_manager import automotive_parts as parts
from autostop_manager.automotive_contracts import binding, ready_identity, result, validate_catalog_context

VIN = "1M8GDM9AXKP042788"  # Public VINinfo/NHTSA documentation example, never a customer record.
OTHER_VIN = "1HGCM82633A004352"  # Public NHTSA example.
REF = {"provider": "partsapi_ru", "namespace": "tecdoc", "entity_kind": "modification", "id": "42", "carType": "PC"}


@pytest.fixture
def forbidden_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("pure operation attempted network or hidden acquisition")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr("autostop_manager.work_pricing._load_labor_experience", denied)
    monkeypatch.setattr("autostop_manager.work_pricing.collect_public_work_pricing_research", denied)
    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", denied)
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", denied)


def test_independent_vpic_reads_only_one_selected_endpoint(monkeypatch):
    calls = []

    def read(vin, **options):
        calls.append((vin, options))
        return {
            "ok": True,
            "outcome": "success",
            "vehicle": {"make": "DEMO"},
            "identifier_binding": {"status": "exact"},
        }

    monkeypatch.setattr(identity.vin_lookup, "decode_vin_vpic", read)
    row = identity.decode_vin_vpic(VIN, timeout_seconds=2)
    assert row["execution"]["network_calls"] == len(calls) == 1
    assert row["data"]["input_binding"] == binding(VIN)
    assert VIN not in str(row)
    assert identity.decode_vin_vpic("DEMO", timeout_seconds=2)["outcome"] == "invalid_input"
    assert len(calls) == 1


def test_independent_vpic_empty_response_keeps_checked_safe_diagnostics(monkeypatch):
    monkeypatch.setattr(
        identity.vin_lookup,
        "decode_vin_vpic",
        lambda *_args, **_kwargs: {
            "ok": False,
            "outcome": "empty_result",
            "vehicle": {},
            "identifier_binding": {"status": "exact", "verified": True},
            "error_codes": ["7", "400", VIN, "7"],
            "error_text": "Unsupported " + VIN,
            "has_error_text": True,
            "diagnostics_status": "reported",
            "coverage": "partial_or_unsupported",
        },
    )
    row = identity.decode_vin_vpic(VIN)
    assert row["outcome"] == "empty" and row["data"]["vehicle_profile"] == {}
    assert row["data"]["diagnostics"] == {
        "error_codes": ["7", "400"],
        "has_error_text": True,
        "diagnostics_status": "reported",
        "coverage": "partial_or_unsupported",
    }
    assert row["data"]["input_binding"] == binding(VIN)
    assert row["execution"]["network_calls"] == 1
    assert VIN not in str(row)


def test_wmi_is_not_model_identity(monkeypatch):
    monkeypatch.setattr(
        identity.vin_lookup,
        "decode_wmi_vpic",
        lambda *args, **kwargs: {
            "ok": True,
            "outcome": "success",
            "wmi_profile": {"name": "Demo manufacturer", "model": "illegal model"},
            "identifier_binding": {"status": "exact"},
        },
    )
    row = identity.decode_wmi_vpic("1M8")
    assert row["data"]["vehicle_profile"]["manufacturer"] == "Demo manufacturer"
    assert "model" not in row["data"]["vehicle_profile"]
    assert identity.decode_wmi_vpic("INVALID")["execution"]["network_calls"] == 0


def test_identifier_inspect_never_substitutes_frame(forbidden_network):
    row = identity.inspect_vehicle_identifier("NZT260-000001", "frame_number")
    assert row["data"]["identifier_kind"] == "frame_number"
    assert row["execution"]["network_calls"] == 0
    assert identity.inspect_vehicle_identifier("NZT260-000001", "vin")["outcome"] == "invalid_input"


def test_reconciliation_preserves_conflict_and_common_lineage(forbidden_network):
    def source(engine, lineage="nhtsa_vpic"):
        return result(
            "demo",
            "success",
            {"vehicle_profile": {"make": "DEMO", "engine": engine}, "input_binding": binding(VIN)},
            evidence=[{"primary_lineage": lineage}],
        )

    row = identity.reconcile_vehicle_identity(VIN, [source("E1"), source("E2")], {"production_date": "2020-01-02"})
    assert "engine" not in row["data"]["vehicle_profile"]
    assert row["data"]["field_statuses"]["engine"] == "disputed"
    assert len(row["data"]["variants"]) == 2
    assert {item["primary_lineage"] for item in row["data"]["provenance"]["engine"]} == {"nhtsa_vpic"}
    assert row["data"]["vehicle_profile"]["production_date"] == "2020-01-02"
    foreign = source("E1")
    foreign["data"]["input_binding"] = binding(OTHER_VIN)
    assert identity.reconcile_vehicle_identity(VIN, [foreign])["conflicts"][0]["code"] == "result_binding_mismatch"


def test_modification_comparison_retains_gearbox_alternatives(forbidden_network):
    rows = [{"make": "DEMO", "model": "M", "transmission": value} for value in ("manual", "automatic")]
    row = identity.compare_vehicle_modifications({"make": "DEMO", "model": "M"}, rows)
    assert row["data"]["selected"] is None
    assert len(row["data"]["alternatives"]) == 2
    assert "transmission" in row["missing_fields"]
    matched = identity.compare_vehicle_modifications({"transmission": "manual"}, rows)
    assert matched["data"]["selected"]["transmission"] == "manual"


def test_multiposition_request_keeps_quantity_side_unknown_and_zero(forbidden_network):
    row = parts.normalize_parts_request(
        "передние колодки 1 комплект; задний левый амортизатор 1 шт; неизвестная прокладка 2 шт"
    )
    items = row["data"]["items"]
    assert len(items) == 3
    assert [(item["quantity"], item["unit"]) for item in items] == [(1, "set"), (1, "piece"), (2, "piece")]
    assert items[1]["side"] == "left" and items[1]["axle"] == "rear"
    assert items[2]["intent"]["intent_id"] == "unknown"
    no_qty = parts.normalize_parts_request(
        items=[{"description": "масляный фильтр", "part_number": "DEMO- 001", "quantity": 0}]
    )["data"]["items"][0]
    assert no_qty["quantity"] == 0 and no_qty["unit"] is None
    assert no_qty["raw_part_number"] == "DEMO- 001" and no_qty["normalized_part_number"] == "DEMO001"
    assert parts.normalize_parts_request(items=[{"quantity": False}])["outcome"] == "invalid_input"


def test_tree_selection_is_bound_to_one_modification(forbidden_network):
    tree = {"modification": REF, "rows": [{"NODE_3_TEXT": "амортизатор", "NODE_3_STR_ID": "18"}]}
    row = parts.resolve_catalog_group(tree, "амортизатор", REF)
    node = row["data"]["nodes"][0]
    assert node["parent"] == REF and node["namespace"] == "tecdoc"
    assert (
        validate_catalog_context(
            "articles", {"carId": "42", "carType": "PC", "strId": "18"}, {"modification": REF, "tree_node": node}
        )
        == []
    )
    other = {**REF, "id": "43"}
    assert parts.resolve_catalog_group(tree, "амортизатор", other)["outcome"] == "invalid_input"
    assert validate_catalog_context(
        "articles", {"carId": "43", "carType": "PC", "strId": "18"}, {"modification": REF, "tree_node": node}
    ) == ["modification_reference_mismatch"]
    wrong_namespace = {**REF, "namespace": "autonorms"}
    assert parts.resolve_catalog_group(tree, "амортизатор", wrong_namespace)["outcome"] == "invalid_input"


@pytest.mark.parametrize(
    "operation,namespace,parameter,key,kind",
    [
        ("toTypes", "maintenance", "modelId", "model", "model"),
        ("toParts", "maintenance", "typeId", "type", "modification"),
        ("norms_motors", "autonorms", "modelId", "model", "model"),
        ("norms_times", "autonorms", "motorId", "motor", "motor"),
    ],
)
def test_directory_ids_do_not_cross_namespaces(operation, namespace, parameter, key, kind):
    reference = {"provider": "partsapi_ru", "namespace": namespace, "entity_kind": kind, "id": 42}
    assert validate_catalog_context(operation, {parameter: 42}, {key: reference}) == []
    reference["namespace"] = "tecdoc"
    assert validate_catalog_context(operation, {parameter: 42}, {key: reference}) == ["invalid_" + key + "_reference"]


def test_article_and_cross_cannot_become_exact_oem(forbidden_network):
    source = {
        "provider": "demo",
        "primary_lineage": "demo",
        "method": "article",
        "locator": "https://example.com/article",
        "document_kind": "article",
        "declares_oem": True,
        "scope": "exact_identifier",
        "fetched_at": "2026-01-01T00:00:00Z",
        "part_number": "DEMO-001",
        "brand": "DEMO",
    }
    row = parts.capture_oem_evidence("DEMO-001", source, "exact_identifier", binding(VIN))
    assert row["data"]["candidate"]["oem_confirmed"] is False
    source.update(document_kind="official_epc", identifier_binding=binding(VIN))
    confirmed = parts.capture_oem_evidence("DEMO-001", source, "exact_identifier", binding(VIN), "DEMO")
    assert confirmed["data"]["candidate"]["oem_confirmed"] is True
    assert confirmed["data"]["candidate"]["fitment_confirmed"] is False
    source.pop("fetched_at")
    assert (
        parts.capture_oem_evidence("DEMO-001", source, "exact_identifier", binding(VIN))["data"]["candidate"][
            "oem_confirmed"
        ]
        is False
    )


def test_supersession_requires_direction_and_primary_source(forbidden_network):
    relation = {
        "from": {"number": "OLD-1", "brand": "DEMO"},
        "to": {"number": "NEW-1", "brand": "DEMO"},
        "type": "supersession",
    }
    row = parts.compare_part_relations([relation])
    assert row["data"]["relations"][0]["type"] == "unverified_supersession"
    relation.update(
        direction="from_to",
        evidence=[
            {
                "document_kind": "manufacturer_supersession",
                "primary_lineage": "manufacturer",
                "locator": "https://example.com/epc",
                "provider": "DEMO",
                "method": "supersessions",
                "fetched_at": "2026-01-01T00:00:00Z",
                "from": deepcopy(relation["from"]),
                "to": deepcopy(relation["to"]),
                "direction": "from_to",
            }
        ],
    )
    alternate = deepcopy(relation)
    alternate["to"]["number"] = "NEW-2"
    alternate["evidence"][0]["to"]["number"] = "NEW-2"
    branches = parts.compare_part_relations([relation, alternate])
    assert branches["conflicts"][0]["code"] == "supersession_branches"
    assert branches["data"]["relations"][0]["fitment_confirmed"] is False


@pytest.mark.parametrize(
    "criteria,profile,expected",
    [
        ({"engine": "E1"}, {}, "unknown"),
        ({"engine": "E1"}, {"engine": "E2"}, "rejected"),
        ({"axle": "front"}, {"axle": "rear"}, "rejected"),
        ({"production_date": {"to": "2020-01-01"}}, {"production_date": "2021-01-01"}, "rejected"),
        ({"displacement_cc": {"from": 1000, "to": 2000}}, {"displacement_cc": 900}, "rejected"),
    ],
)
def test_fitment_unknown_and_mismatch_are_never_supported(forbidden_network, criteria, profile, expected):
    row = parts.assess_part_fitment(profile, {"number": "DEMO", "brand": "DEMO"}, criteria, [], "exact_identifier")
    assert row["data"]["state"] == expected


def test_exact_fitment_requires_bound_primary_assertion(forbidden_network):
    vehicle = {"vehicle_profile": {"engine": "E1"}, "input_binding": binding(VIN)}
    part = {"number": "DEMO-1", "brand": "DEMO"}
    source = {
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "primary_lineage": "DEMO manufacturer",
        "provider": "DEMO",
        "method": "epc_fitment",
        "fetched_at": "2026-01-01T00:00:00Z",
        "locator": "https://example.com/epc",
        "part_number": "DEMO1",
        "brand": "DEMO",
        "scope": "exact_identifier",
        "identifier_binding": binding(VIN),
    }
    assert (
        parts.assess_part_fitment(vehicle, part, {"engine": "E1"}, [source], "exact_identifier")["data"]["state"]
        == "supported"
    )
    source["identifier_binding"] = binding(OTHER_VIN)
    assert (
        parts.assess_part_fitment(vehicle, part, {"engine": "E1"}, [source], "exact_identifier")["data"]["state"]
        == "unknown"
    )


def test_labor_raw_time_units_and_unknown_survive(forbidden_network):
    row = labor.normalize_labor_time(
        [
            {"workName": "demo", "workTime": 120, "unit": "minutes", "included_operations": ["wash"]},
            {"workName": "unknown", "workTime": 2},
        ],
        source={"provider": "autonorms"},
    )
    assert row["data"]["labor"][0]["hours"] == 2
    assert row["data"]["labor"][0]["raw_time"] == 120
    assert row["data"]["labor"][1]["hours"] is None
    assert row["data"]["overlaps"][0]["included_operations"] == ["wash"]


def test_price_is_pure_and_unknown_costs_are_not_zero(forbidden_network):
    policy = {"basis": "hourly_rate", "version": "demo-v1", "rounding": 100}
    row = labor.calculate_work_price([{"operation_name": "demo", "hours": 2}], policy, hourly_rate=1000)
    assert row["data"]["total"] == 2000
    unknown = labor.calculate_work_price(
        [{"operation_name": "demo", "hours": 2}], policy, hourly_rate=1000, unknown_costs=["diagnostics"]
    )
    assert unknown["data"]["total"] is None and unknown["data"]["known_subtotal"] == 2000
    assert unknown["execution"]["network_calls"] == 0
    missing = labor.calculate_work_price([{"operation_name": "demo", "hours": None}], policy, hourly_rate=1000)
    assert missing["data"]["total"] is None
    assert labor.calculate_work_price([], {"basis": "hourly_rate"}, hourly_rate=1000)["outcome"] == "invalid_input"


def test_calculation_does_not_double_count_included_and_overlap(forbidden_network):
    rows = [
        {"operation_id": "A", "hours": 2, "included_operations": ["B"]},
        {"operation_id": "B", "hours": 1},
        {"operation_id": "C", "hours": 1, "overlaps_with": ["A"]},
    ]
    row = labor.calculate_work_price(rows, {"basis": "hourly_rate", "version": "1"}, hourly_rate=1000)
    assert row["data"]["known_subtotal"] == 2000
    assert {item["reason"] for item in row["data"]["exclusions"]} == {
        "duplicate_or_included_operation",
        "unresolved_overlap",
    }


def test_public_evidence_acquisition_explicit_and_bounded(monkeypatch):
    from autostop_manager import work_pricing_research as research

    calls = []
    monkeypatch.setattr(
        research, "_ddg_search", lambda query, **kwargs: calls.append((query, kwargs)) or {"results": []}
    )
    row = labor.collect_work_price_evidence(["замена масла"], {"make": "DEMO"}, max_queries=2)
    assert row["execution"]["network_calls"] == len(calls) == 2
    aggregate = labor.collect_work_price_evidence(
        ["замена масла"], sources=["provided_aggregate"], aggregate_evidence={"count": 3}
    )
    assert aggregate["execution"]["network_calls"] == 0 and len(calls) == 2
    assert labor.collect_work_price_evidence([VIN])["outcome"] == "invalid_input"


def test_public_identity_reuse_rejects_foreign_binding_before_any_decode(forbidden_network):
    from autostop_manager.vehicle_identity import decode_vehicle_identity
    from autostop_manager.vin_lookup import lookup_original_parts
    from autostop_manager.vin_oem_resolver import resolve_vin_oem_parts

    ready = decode_vehicle_identity(VIN, crm_context={"make": "DEMO"}, live_vpic=False, live_wmi=False)
    dossier = lookup_original_parts(VIN, vehicle_identity=ready)
    assert dossier["decoded_vehicle"]["make"] == "DEMO"
    assert lookup_original_parts(OTHER_VIN, vehicle_identity=ready)["error"] == "invalid_ready_identity"
    resolved = resolve_vin_oem_parts(identifier=VIN, requested_part="масляный фильтр", vehicle_identity=ready)
    assert resolved["calls"] == []
    foreign = resolve_vin_oem_parts(identifier=OTHER_VIN, requested_part="масляный фильтр", vehicle_identity=ready)
    assert foreign["calls"] == []


def test_ready_price_evidence_does_not_research_or_load_experience(forbidden_network):
    from autostop_manager.work_pricing import estimate_repair_work_cost

    evidence = {"work_items": ["замена масла"], "observations": [], "labor": [], "vehicle_context": {"make": "DEMO"}}
    row = estimate_repair_work_cost(work_items=["замена масла"], make="DEMO", price_evidence=evidence)
    assert row["ok"] is True
    assert (
        estimate_repair_work_cost(work_items=["иная работа"], price_evidence=evidence)["error"]
        == "invalid_ready_price_evidence"
    )


def test_selected_batch_never_switches_decoder(monkeypatch):
    calls = []
    monkeypatch.setattr(
        identity,
        "decode_vin_vpic",
        lambda identifier, **kwargs: calls.append(identifier) or result("decode_vin_vpic", "empty", {}),
    )
    row = identity.decode_vehicle_batch([VIN, {"bad": "row"}, OTHER_VIN], "decode_vin_vpic")
    assert calls == [VIN, OTHER_VIN]
    assert [item["item_index"] for item in row["data"]["items"]] == [0, 1, 2]
    assert row["data"]["items"][1]["result"]["outcome"] == "invalid_input"


def test_selected_batch_row_exception_does_not_lose_remaining_items(monkeypatch):
    def decode(identifier, **kwargs):
        if identifier == "BAD":
            raise OSError("transport failure")
        return result("decode_vin_vpic", "success", {})

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)
    row = identity.decode_vehicle_batch([VIN, "BAD", OTHER_VIN], "decode_vin_vpic")
    assert [item["result"]["outcome"] for item in row["data"]["items"]] == ["success", "provider_error", "success"]


def test_async_batch_cancellation_prevents_next_row(monkeypatch):
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    calls = []

    def decode(identifier, **kwargs):
        calls.append(identifier)
        entered.set()
        release.wait(timeout=2)
        return result("decode_vin_vpic", "success", {})

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)

    async def scenario():
        task = asyncio.create_task(identity.decode_vehicle_batch_async([VIN, OTHER_VIN], "decode_vin_vpic"))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert calls == [VIN]


@pytest.mark.parametrize(
    "call",
    [
        lambda: parts.assess_part_fitment({"conflicts": "malformed"}, {"number": "DEMO"}, {}, []),
        lambda: parts.compare_part_relations([{"from": {"number": 123}, "to": None}]),
        lambda: identity.compare_vehicle_modifications({}, [None]),
        lambda: parts.resolve_catalog_group({"rows": [None]}, "demo", REF),
        lambda: parts.capture_oem_evidence("DEMO", {}, "exact_identifier", {"fake": "digest"}),
    ],
)
def test_malformed_nested_inputs_do_not_raise_or_confirm(forbidden_network, call):
    row = call()
    assert row["outcome"] in {"invalid_input", "partial"}
    assert row["execution"]["network_calls"] == 0


@pytest.mark.parametrize("failure", ["provider_error", "mismatch", "conflict"])
def test_ready_and_reconciled_identity_do_not_promote_failed_provider(forbidden_network, failure):
    from autostop_manager.vin_lookup import lookup_original_parts

    row = result("decode_vin_vpic", "success", {"vehicle_profile": {"engine": "E1"}, "input_binding": binding(VIN)})
    if failure == "provider_error":
        row["ok"], row["outcome"] = False, "provider_error"
    elif failure == "mismatch":
        row["data"]["identifier_binding"] = {"status": "mismatch"}
    else:
        row["conflicts"] = [{"field": "engine", "code": "upstream_disputed"}]
    assert ready_identity(row, VIN)[0] is None
    assert lookup_original_parts(VIN, vehicle_identity=row)["error"] == "invalid_ready_identity"
    merged = identity.reconcile_vehicle_identity(VIN, [row])
    assert "engine" not in merged["data"]["vehicle_profile"]
    assert merged["conflicts"]
    if failure == "conflict":
        assert merged["conflicts"][0]["code"] == "upstream_disputed"


def test_reconciliation_keeps_upstream_alternatives_and_provider_context_lineage(forbidden_network):
    from autostop_manager.tecdoc_vehicle_selection import _independent_values
    from autostop_manager.vehicle_identity import decode_vehicle_identity

    child = result(
        "demo",
        "partial",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": {"make": "DEMO"},
            "model_year_candidates": [1990, 2020],
            "variants": [{"profile": {"engine": "E1"}}, {"profile": {"engine": "E2"}}],
        },
    )
    context = {"provider": "partsapi_ru", "vehicle_profile": {"make": "DEMO", "engine": "E1"}}
    merged = identity.reconcile_vehicle_identity(VIN, [child], context)
    assert merged["data"]["model_year_candidates"] == [1990, 2020]
    assert {item["profile"].get("engine") for item in merged["data"]["variants"]} >= {"E1", "E2"}
    assert {item["primary_lineage"] for item in merged["data"]["provenance"]["engine"]} == {"partsapi_ru"}
    assert not _independent_values({}, context)["engine"]
    copied = decode_vehicle_identity(VIN, crm_context=context, live_vpic=False, live_wmi=False)
    assert not _independent_values(copied, {})["engine"]


def test_reconciliation_preserves_composite_candidate_levels_origins_years_and_country(forbidden_network):
    profile = {
        "make": "DEMO",
        "manufacturer": "Demo manufacturer",
        "market": "EU",
        "country": "Country A",
        "manufacturer_country": "Country B",
    }
    origins = [
        {
            "field": field,
            "value": value,
            "source": "local WMI hint",
            "strength": "candidate",
            "bound": False,
            "source_kind": "local_hint",
            "depends_on": ["local_registry"],
            "independent": True,
            "raw_value": value,
            "normalization": {"method": "local_rules"},
        }
        for field, value in profile.items()
    ]
    ready = {
        "ok": True,
        "input_binding": binding(VIN),
        "vehicle_profile": profile,
        "field_statuses": {field: {"status": "candidate"} for field in profile},
        "field_evidence": origins,
        "diagnostics": {"model_year": {"candidate_years": [1981, 2011]}},
        "model_year_candidates": [2011],
        "scope": "family",
    }
    before = deepcopy(ready)
    merged = identity.reconcile_vehicle_identity(VIN, [ready])["data"]
    assert merged["vehicle_profile"] == profile
    assert all(merged["field_statuses"][field] == "candidate" for field in profile)
    assert merged["model_year_candidates"] == [2011, 1981]
    assert merged["diagnostics"]["model_year"]["candidate_years"] == [2011, 1981]
    assert "model_year" not in merged["vehicle_profile"]
    assert merged["variants"][0]["scope"] == "family"
    assert merged["variants"][0]["diagnostics"] == ready["diagnostics"]
    for field in profile:
        origin = merged["provenance"][field][0]
        assert origin == {
            **next(row for row in origins if row["field"] == field),
            "primary_lineage": "autostop_local_vehicle_rules",
            "result_index": 0,
        }
    assert not merged["parts_lookup_readiness"]["exact_applicability_confirmed"]
    assert ready == before


def test_reconciliation_repeated_wmi_primary_source_stays_candidate(forbidden_network):
    def source(lineage):
        return result(
            "demo",
            "partial",
            {
                "wmi": VIN[:3],
                "input_binding": None,
                "vehicle_profile": {"make": "DEMO", "engine": "UNSAFE"},
                "diagnostics": {"model_year": {"candidate_years": [2011]}},
                "field_statuses": {"make": {"status": "candidate"}},
                "field_evidence": [
                    {
                        "field": "make",
                        "value": "DEMO",
                        "source": lineage,
                        "source_kind": "local_hint",
                        "strength": "candidate",
                        "bound": False,
                    }
                ],
            },
        )

    merged = identity.reconcile_vehicle_identity(VIN, [source("NHTSA vPIC WMI"), source("nhtsa_vpic_via_corgi")])[
        "data"
    ]
    assert merged["field_statuses"]["make"] == "candidate"
    assert "engine" not in merged["vehicle_profile"]
    assert merged["model_year_candidates"] == []
    assert {row["primary_lineage"] for row in merged["provenance"]["make"]} == {"nhtsa_vpic"}
    assert all(row["strength"] == "candidate" and row["bound"] is False for row in merged["provenance"]["make"])
    assert not merged["parts_lookup_readiness"]["exact_applicability_confirmed"]


def test_reconciliation_keeps_supported_origin_and_child_variant_lineage_scope(forbidden_network):
    child = {
        "profile": {"engine": "E2"},
        "source_lineage": "vininfo_local_rules",
        "scope": "decoded_fields",
        "model_year_candidates": [1981, 2011],
    }
    origin = {
        "field": "engine",
        "value": "E1",
        "primary_lineage": "nhtsa_vpic",
        "strength": "supported",
        "source_kind": "provider",
        "bound": True,
        "depends_on": [],
        "independent": True,
    }
    ready = result(
        "demo",
        "partial",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": {"engine": "E1"},
            "scope": "family",
            "field_statuses": {"engine": {"status": "supported"}},
            "field_evidence": [origin],
            "variants": [child],
            "family_candidates": [child],
        },
    )
    merged = identity.reconcile_vehicle_identity(VIN, [ready])["data"]
    assert merged["field_statuses"]["engine"] == "supported"
    assert merged["provenance"]["engine"][0] == {**origin, "result_index": 0}
    assert merged["variants"].count({**child, "result_index": 0}) == 1
    assert not merged["parts_lookup_readiness"]["exact_applicability_confirmed"]


@pytest.mark.parametrize("failure", ["provider_error", "mismatch", "disputed"])
def test_reconciliation_failed_or_foreign_results_do_not_supply_years_or_variants(forbidden_network, failure):
    rejected = result(
        "demo",
        "partial",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": {"engine": "UNSAFE"},
            "model_year_candidates": [1981],
            "diagnostics": {"model_year": {"candidate_years": [2011]}},
            "variants": [{"profile": {"engine": "UNSAFE"}, "source_lineage": "rejected_source"}],
        },
    )
    if failure == "provider_error":
        rejected["ok"], rejected["outcome"] = False, "provider_error"
    elif failure == "mismatch":
        rejected["data"]["input_binding"] = binding(OTHER_VIN)
    else:
        rejected["data"]["field_statuses"] = {"engine": {"status": "disputed"}}
    accepted = result(
        "demo",
        "partial",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": {"make": "DEMO"},
            "model_year_candidates": [2020],
        },
    )
    merged = identity.reconcile_vehicle_identity(VIN, [rejected, accepted])["data"]
    assert "engine" not in merged["vehicle_profile"]
    assert merged["model_year_candidates"] == [2020]
    assert merged["diagnostics"]["model_year"]["candidate_years"] == [2020]
    assert all(item["result_index"] == 1 for item in merged["variants"])
    assert merged["conflicts"]
    if failure == "disputed":
        assert merged["field_statuses"]["engine"] == "disputed"
    assert not merged["parts_lookup_readiness"]["exact_applicability_confirmed"]


@pytest.mark.parametrize(
    "change",
    [
        {"part_number": "OTHER-2"},
        {"brand": "OTHER"},
        {"fetched_at": "not-a-timestamp"},
        {"fetched_at": "2026-01-01T00:00:00"},
    ],
)
def test_oem_confirmation_requires_related_article_brand_and_aware_time(forbidden_network, change):
    source = {
        "provider": "DEMO",
        "primary_lineage": "DEMO",
        "method": "epc",
        "locator": "https://example.com/epc",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "declares_oem": True,
        "part_number": "DEMO-1",
        "brand": "DEMO",
        "scope": "exact_identifier",
        "identifier_binding": binding(VIN),
        **change,
    }
    row = parts.capture_oem_evidence("DEMO-1", source, "exact_identifier", binding(VIN), "DEMO")
    assert row["data"]["candidate"]["oem_confirmed"] is False


def test_fitment_checks_primary_conditions_provenance_and_typed_modification(forbidden_network):
    source = {
        "provider": "DEMO",
        "primary_lineage": "DEMO",
        "method": "epc",
        "locator": "https://example.com/epc",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "part_number": "DEMO1",
        "brand": "DEMO",
        "scope": "exact_identifier",
        "identifier_binding": binding(VIN),
        "conditions": {"engine": "E2"},
    }
    vehicle = {"input_binding": binding(VIN), "engine": "E1"}
    part = {"number": "DEMO1", "brand": "DEMO"}
    assert parts.assess_part_fitment(vehicle, part, {}, [source], "exact_identifier")["data"]["state"] == "rejected"
    source["conditions"] = {"transmission": "automatic"}
    assert parts.assess_part_fitment(vehicle, part, {}, [source], "exact_identifier")["data"]["state"] == "unknown"
    source.pop("conditions")
    for field in ("provider", "method", "fetched_at"):
        malformed = {key: value for key, value in source.items() if key != field}
        assert (
            parts.assess_part_fitment(vehicle, part, {}, [malformed], "exact_identifier")["data"]["state"] == "unknown"
        )
    source.update(scope="modification", catalog_ref="arbitrary")
    vehicle["catalog_ref"] = "arbitrary"
    assert parts.assess_part_fitment(vehicle, part, {}, [source])["data"]["state"] == "unknown"
    source["catalog_ref"], vehicle["catalog_ref"] = REF, REF
    assert parts.assess_part_fitment(vehicle, part, {}, [source])["data"]["state"] == "supported"


def test_direct_adapter_rejects_bad_ids_and_tree_binding_before_request(monkeypatch, forbidden_network):
    from autostop_manager.catalog_clients import partsapi_catalog_lookup

    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://example.com/api")
    monkeypatch.setenv("PARTSAPI_SHOP_KEY", "dummy")
    for options in ({"type_id": True}, {"type_id": 0}, {"type_id": 42, "vehicle_type": "unknown"}):
        row = partsapi_catalog_lookup(operation="search_tree", dry_run=True, **options)
        assert row["outcome"] == "invalid_input" and row["attempt_count"] == 0
    node = {**REF, "entity_kind": "tree_node", "id": 18, "parent": REF, "tree_sha256": "x"}
    bound = partsapi_catalog_lookup(
        operation="articles",
        type_id="42",
        category="18",
        catalog_context={"modification": REF, "tree_node": node},
        dry_run=True,
    )
    assert bound["outcome"] == "invalid_input"
    legacy = partsapi_catalog_lookup(operation="articles", type_id="42", category="18", dry_run=True)
    assert legacy["catalog_binding"]["status"] == "raw_parameters_unverified"
    assert legacy["catalog_binding"]["category_queryable"] is False
    assert legacy["catalog_binding"]["exact_vehicle_confirmed"] is False


def test_supersession_rejects_unrelated_document_endpoints(forbidden_network):
    relation = {
        "type": "supersession",
        "direction": "from_to",
        "from": {"number": "DEMO1", "brand": "DEMO"},
        "to": {"number": "DEMO2", "brand": "DEMO"},
        "evidence": [
            {
                "document_kind": "manufacturer_supersession",
                "provider": "DEMO",
                "primary_lineage": "DEMO",
                "method": "epc",
                "fetched_at": "2026-01-01T00:00:00Z",
                "locator": "https://example.com/epc",
                "direction": "from_to",
                "from": {"number": "OTHER1", "brand": "DEMO"},
                "to": {"number": "OTHER2", "brand": "DEMO"},
            }
        ],
    }
    assert parts.compare_part_relations([relation])["data"]["relations"][0]["type"] == "unverified_supersession"


@pytest.mark.parametrize("bounds", [[2, None], [2, 1], [False, 2], [1, True], None, "unknown"])
def test_explicit_unknown_labor_range_never_falls_back_to_exact_hours(forbidden_network, bounds):
    row = labor.calculate_work_price(
        [{"operation_name": "demo", "hours": 1, "range_hours": bounds}],
        {"basis": "hourly_rate", "version": "1"},
        hourly_rate=1000,
    )
    assert row["data"]["total"] is None and row["missing_fields"]


@pytest.mark.parametrize(
    "bounds,expected_range,expected_total",
    [
        (["1,5", "2,5"], [1500, 2500], 2000),
        (["1.5", "2,5"], [1500, 2500], 2000),
        ([1.5, 2.5], [1500, 2500], 2000),
        (["0,0", "0"], [0, 0], 0),
    ],
)
def test_labor_price_uses_the_validated_numeric_range(forbidden_network, bounds, expected_range, expected_total):
    row = labor.calculate_work_price(
        [{"operation_name": "demo", "range_hours": bounds}],
        {"basis": "hourly_rate", "version": "1"},
        hourly_rate=1000,
    )
    assert row["outcome"] == "success"
    assert row["data"]["range"] == row["data"]["operations"][0]["range"] == expected_range
    assert row["data"]["total"] == expected_total
    assert row["execution"]["network_calls"] == 0


def test_explicit_null_quantity_stays_unknown(forbidden_network):
    row = parts.normalize_parts_request(items=[{"description": "масляный фильтр", "quantity": None}])
    assert row["outcome"] == "partial"
    assert row["data"]["items"][0]["quantity"] is None
    assert row["data"]["items"][0]["quantity_status"] == "unknown"


@pytest.mark.parametrize(
    "field,expected,actual,state",
    [
        ("production_date", {"from": "2000-01-01"}, "unknown", "unknown"),
        ("production_date", {"from": "unknown"}, "2020-01-01", "unknown"),
        ("production_date", {"from": "2020-02-15"}, "2020-02", "unknown"),
        ("production_date", {"from": "2020-02-01", "to": "2020-02-29"}, "2020-02", "supported"),
        ("production_date", {"from": "2020-03-01"}, "2020-02", "rejected"),
        ("model_year", {"from": 2000}, "unknown", "unknown"),
        ("model_year", {"from": 2000}, 2020.5, "unknown"),
        ("engine", None, "E1", "unknown"),
        ("displacement_cc", {"from": None, "to": None}, 2000, "unknown"),
    ],
)
def test_fitment_range_preserves_invalid_and_incomplete_discriminators(
    forbidden_network, field, expected, actual, state
):
    assert parts._criterion(field, expected, actual) == state


@pytest.mark.parametrize("mutation", ["provider_error", "conflict", "malformed_labor"])
def test_ready_price_rejects_failed_or_conflicting_envelope_before_acquisition(forbidden_network, mutation):
    from autostop_manager.work_pricing import estimate_repair_work_cost

    ready = result(
        "collect_work_price_evidence",
        "success",
        {"work_items": ["замена масла"], "observations": [], "labor": [], "vehicle_context": {}},
    )
    if mutation == "provider_error":
        ready["ok"], ready["outcome"] = False, "provider_error"
    elif mutation == "conflict":
        ready["conflicts"] = [{"field": "price", "code": "disputed"}]
    else:
        ready["data"]["labor"] = [None]
    assert (
        estimate_repair_work_cost(work_items=["замена масла"], price_evidence=ready)["error"]
        == "invalid_ready_price_evidence"
    )


@pytest.mark.parametrize("parameter", ["top_category_id", "sub_category_id"])
def test_norms_direct_category_ids_use_actual_provider_names(monkeypatch, forbidden_network, parameter):
    from autostop_manager.catalog_clients import partsapi_catalog_lookup

    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://example.com/api")
    monkeypatch.setenv("PARTSAPI_GET_NORMS_TIMES_KEY", "synthetic-key")
    options = {"operation": "norms_times", "motor_id": 42, "top_category_id": 0, "sub_category_id": 0, "dry_run": True}
    for value in (True, False, -1, "not-an-id"):
        rejected = partsapi_catalog_lookup(**{**options, parameter: value})
        assert rejected["outcome"] == "invalid_input" and rejected["attempt_count"] == 0
    zero = partsapi_catalog_lookup(**options)
    assert zero["outcome"] == "configured_unverified"
    assert zero["attempt_count"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"ok": False, "outcome": "provider_error"},
        {"conflicts": [{"field": "make", "code": "disputed"}]},
        {"field_statuses": {"engine": "disputed"}},
        {"field_statuses": {"engine": {"status": "disputed"}}},
        {"conflicts": False},
    ],
)
@pytest.mark.parametrize("envelope", [False, True])
def test_bound_fitment_never_promotes_failed_or_disputed_identity(forbidden_network, change, envelope):
    vehicle = {"input_binding": binding(VIN), "vehicle_profile": {"engine": "E1"}, **change}
    if envelope:
        vehicle = {"data": vehicle, "outcome": "partial"}
    source = {
        "provider": "DEMO",
        "primary_lineage": "DEMO",
        "method": "epc",
        "locator": "https://example.com/epc",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "part_number": "DEMO1",
        "brand": "DEMO",
        "scope": "exact_identifier",
        "identifier_binding": binding(VIN),
    }
    row = parts.assess_part_fitment(
        vehicle, {"number": "DEMO1", "brand": "DEMO"}, {"engine": "E1"}, [source], "exact_identifier"
    )
    assert row["data"]["state"] == "conflict"
    assert row["outcome"] == "partial" and row["conflicts"]
    assert row["execution"]["network_calls"] == 0


@pytest.fixture
def bound_fitment_source():
    return {
        "provider": "DEMO",
        "primary_lineage": "DEMO",
        "method": "epc",
        "locator": "https://example.com/epc",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "part_number": "DEMO1",
        "brand": "DEMO",
        "scope": "exact_identifier",
        "identifier_binding": binding(VIN),
    }


@pytest.mark.parametrize("level", ["raw", "outer", "inner"])
@pytest.mark.parametrize(
    "provider_binding",
    [
        {"status": "mismatch", "verified": False},
        {"status": "exact", "verified": False},
        {"status": "bound", "verified": 0},
        {"status": "matched", "verified": None},
        {"status": "exact", "verified": 1},
        {"status": "exact", "verified": "true"},
    ],
)
def test_provider_binding_failure_on_either_level_blocks_all_identity_consumers(
    forbidden_network, bound_fitment_source, level, provider_binding
):
    from autostop_manager.vin_lookup import lookup_original_parts

    data = {"input_binding": binding(VIN), "vehicle_profile": {"engine": "E1"}}
    vehicle = data if level == "raw" else result("demo", "success", data)
    target = data if level in {"raw", "inner"} else vehicle
    target["identifier_binding"] = provider_binding
    original = deepcopy(vehicle)

    accepted, errors = ready_identity(vehicle, VIN)
    assert accepted is None and errors[0]["code"] == "provider_identifier_unverified"
    assert lookup_original_parts(VIN, vehicle_identity=vehicle)["error"] == "invalid_ready_identity"
    reconciled = identity.reconcile_vehicle_identity(VIN, [vehicle])
    assert "engine" not in reconciled["data"]["vehicle_profile"]
    assert reconciled["conflicts"] and reconciled["execution"]["network_calls"] == 0
    fitment = parts.assess_part_fitment(
        vehicle, {"number": "DEMO1", "brand": "DEMO"}, {"engine": "E1"}, [bound_fitment_source], "exact_identifier"
    )
    assert fitment["data"]["state"] == "conflict" and fitment["data"]["checked_conditions"] == []
    assert fitment["execution"]["network_calls"] == 0
    assert vehicle == original


def test_conflicting_envelope_request_binding_blocks_identity_and_exact_fitment(
    forbidden_network, bound_fitment_source
):
    vehicle = result("demo", "success", {"input_binding": binding(VIN), "vehicle_profile": {"engine": "E1"}})
    vehicle["input_binding"] = binding(OTHER_VIN)
    accepted, errors = ready_identity(vehicle, VIN)
    assert accepted is None and errors == [{"field": "identifier", "code": "ready_identity_binding_conflict"}]
    reconciled = identity.reconcile_vehicle_identity(VIN, [vehicle])
    assert "engine" not in reconciled["data"]["vehicle_profile"] and reconciled["conflicts"]
    fitment = parts.assess_part_fitment(
        vehicle, {"number": "DEMO1", "brand": "DEMO"}, {"engine": "E1"}, [bound_fitment_source], "exact_identifier"
    )
    assert fitment["data"]["state"] == "conflict"


@pytest.mark.parametrize("status", ["exact", "bound", "matched"])
@pytest.mark.parametrize("verified", [None, True], ids=["legacy-omitted", "verified"])
def test_supported_provider_binding_keeps_legacy_compatibility(
    forbidden_network, bound_fitment_source, status, verified
):
    provider_binding = {"status": status}
    if verified is not None:
        provider_binding["verified"] = verified
    vehicle = result(
        "demo",
        "success",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": {"engine": "E1"},
            "identifier_binding": provider_binding,
        },
    )
    vehicle["input_binding"] = binding(VIN)
    vehicle["identifier_binding"] = dict(provider_binding)
    assert ready_identity(vehicle, VIN)[1] == []
    assert identity.reconcile_vehicle_identity(VIN, [vehicle])["data"]["vehicle_profile"]["engine"] == "E1"
    fitment = parts.assess_part_fitment(
        vehicle, {"number": "DEMO1", "brand": "DEMO"}, {"engine": "E1"}, [bound_fitment_source], "exact_identifier"
    )
    assert fitment["data"]["state"] == "supported"


@pytest.mark.parametrize(
    "field,value",
    [
        ("included_operations", None),
        ("included_operations", False),
        ("included_operations", "B"),
        ("included_operations", [False]),
        ("overlaps_with", 1),
        ("overlaps_with", {}),
        ("overlap_resolved", 0),
        ("overlap_resolved", None),
    ],
)
def test_labor_structure_is_validated_before_normalization_or_calculation(forbidden_network, field, value):
    row = {"operation_name": "DEMO", "hours": 1, field: value}
    assert labor.normalize_labor_time([row], source={"provider": "DEMO"})["outcome"] == "invalid_input"
    assert (
        labor.calculate_work_price([row], {"basis": "hourly_rate", "version": "1"}, hourly_rate=1000)["outcome"]
        == "invalid_input"
    )


@pytest.mark.parametrize("source", [False, 0, 1, 1.5, []])
def test_labor_provenance_rejects_scalars_instead_of_asserting_success(forbidden_network, source):
    assert (
        labor.normalize_labor_time([{"operation_name": "DEMO", "hours": 1, "source": source}])["outcome"]
        == "invalid_input"
    )


@pytest.mark.parametrize("source", [None, {}, {"provider": None}, ""])
def test_empty_labor_provenance_remains_partial_without_requiring_new_metadata(forbidden_network, source):
    row = labor.normalize_labor_time([{"operation_name": "DEMO", "hours": 1, "source": source}])
    assert row["outcome"] == "partial" and row["missing_fields"] == ["rows[0].source"]
    minimal = labor.normalize_labor_time([{"operation_name": "DEMO", "hours": 1}], source={"provider": "autonorms"})
    assert minimal["outcome"] == "success"


@pytest.mark.parametrize("hours,total,outcome", [(0, 0, "success"), (False, None, "partial"), (None, None, "partial")])
def test_zero_labor_is_known_but_boolean_and_null_do_not_become_zero(forbidden_network, hours, total, outcome):
    row = labor.calculate_work_price(
        [{"operation_name": "DEMO", "hours": hours}], {"basis": "hourly_rate", "version": "1"}, hourly_rate=1000
    )
    assert row["outcome"] == outcome and row["data"]["total"] == total


def test_reconciliation_retains_field_origins_from_legacy_ready_identity(forbidden_network):
    from autostop_manager.vehicle_identity import decode_vehicle_identity

    ready = decode_vehicle_identity(
        VIN,
        crm_context={"make": "DEMO", "model": "MODEL", "engine": "E1", "source_summary": "PartsAPI"},
        live_vpic=False,
        live_wmi=False,
    )
    row = identity.reconcile_vehicle_identity(VIN, [ready])
    for field in ("make", "model", "engine"):
        assert {origin["primary_lineage"] for origin in row["data"]["provenance"][field]} == {"partsapi_ru"}
        assert not any(origin["independent"] for origin in row["data"]["provenance"][field])


def test_mixed_summary_never_assigns_all_sources_to_each_field(forbidden_network):
    ready = result(
        "demo",
        "success",
        {"input_binding": binding(VIN), "vehicle_profile": {"engine": "E1"}},
        evidence=[{"primary_lineage": "partsapi_ru"}, {"primary_lineage": "nhtsa_vpic"}],
    )
    row = identity.reconcile_vehicle_identity(VIN, [ready])
    assert row["data"]["provenance"]["engine"] == [
        {"value": "E1", "primary_lineage": None, "independent": False, "result_index": 0}
    ]


@pytest.mark.parametrize("source", [{"provider": "DEMO"}, ["DEMO"]])
def test_structured_unknown_primary_source_does_not_crash_or_become_a_fact(forbidden_network, source):
    ready = result(
        "demo",
        "success",
        {"input_binding": binding(VIN), "vehicle_profile": {"engine": "E1"}},
        evidence=[{"primary_lineage": source}],
    )
    row = identity.reconcile_vehicle_identity(VIN, [ready])
    assert row["data"]["provenance"]["engine"][0]["primary_lineage"] is None
    assert row["data"]["provenance"]["engine"][0]["independent"] is False


def test_zero_operation_id_matches_included_references_and_boolean_id_is_rejected(forbidden_network):
    normalized = labor.normalize_labor_time(
        [{"operation_id": 0, "operation_name": "DEMO", "hours": 1}], source={"provider": "DEMO"}
    )
    assert normalized["data"]["labor"][0]["operation_id"] == "0"
    rows = [{"operation_id": "A", "hours": 2, "included_operations": [0]}, {"operation_id": 0, "hours": 1}]
    priced = labor.calculate_work_price(rows, {"basis": "hourly_rate", "version": "1"}, hourly_rate=1000)
    assert priced["data"]["total"] == 2000
    assert priced["data"]["exclusions"] == [{"operation_id": "0", "reason": "duplicate_or_included_operation"}]
    rows[1]["operation_id"] = False
    assert (
        labor.calculate_work_price(rows, {"basis": "hourly_rate", "version": "1"}, hourly_rate=1000)["outcome"]
        == "invalid_input"
    )


@pytest.mark.parametrize("evidence", [[None], None, False, {}, ["source"], [{}] * 501])
def test_catalog_group_rejects_malformed_evidence_before_result_construction(forbidden_network, evidence):
    tree = {
        "rows": [{"NODE_3_STR_ID": "18", "NODE_3_TEXT": "масляный фильтр"}],
        "modification": REF,
        "evidence": evidence,
    }
    row = parts.resolve_catalog_group(tree, "масляный фильтр", REF, 18)
    assert row["outcome"] == "invalid_input"
    assert row["missing_fields"] == ["tree.evidence"]
    assert row["execution"]["network_calls"] == 0


def test_catalog_group_keeps_existing_evidence_without_requiring_new_metadata(forbidden_network):
    evidence = {"source": "legacy PartsAPI tree", "method": "getSearchTree", "legacy_locator": "provided tree"}
    tree = {
        "rows": [{"NODE_3_STR_ID": "18", "NODE_3_TEXT": "масляный фильтр"}],
        "modification": REF,
        "evidence": [evidence],
    }
    row = parts.resolve_catalog_group(tree, "масляный фильтр", REF, 18)
    assert row["outcome"] == "success"
    assert len(row["evidence"]) == 1
    assert all(row["evidence"][0][key] == value for key, value in evidence.items())
    assert tree["evidence"] == [evidence]
    tree.pop("evidence")
    assert parts.resolve_catalog_group(tree, "масляный фильтр", REF, 18)["evidence"] == []


@pytest.mark.parametrize("independent", [False, True])
def test_acquisition_to_legacy_price_preserves_metadata_and_source_independence(
    monkeypatch, forbidden_network, independent
):
    from autostop_manager import work_pricing, work_pricing_research as research

    def search(query, **kwargs):
        return {
            "results": [
                {
                    "source": f"sto-{index if independent else 0}.example",
                    "url": f"https://sto-{index if independent else 0}.example/prices",
                    "title": "Замена масла",
                    "snippet": f"Красноярск, только работа {1000 + 100 * index} руб. Норматив времени 1.5 часа.",
                }
                for index in range(3)
            ]
        }

    monkeypatch.setattr(research, "_ddg_search", search)
    evidence = labor.collect_work_price_evidence(["замена масла"], {"make": "DEMO"}, max_queries=4)
    quote = work_pricing._normalize_quote(evidence["data"]["observations"][0])
    hours = work_pricing._normalize_labor_time_row(evidence["data"]["labor"][0])
    assert quote["source"] == "sto-0.example" and quote["city_region"] == "Красноярск"
    assert quote["captured_at"] == quote["evidence_source"]["fetched_at"]
    assert hours["valid_input"] and hours["public_source"] and hours["range_hours"] == [1.5, 1.5]
    estimated = work_pricing.estimate_repair_work_cost(
        work_items=["замена масла"], make="DEMO", price_evidence=evidence
    )
    operation = estimated["operation_estimates"][0]
    if independent:
        assert operation["pricing_method"] == "krasnoyarsk_market_mean"
        assert estimated["autostop_price_rub"] is not None
    else:
        assert operation["pricing_method"] != "krasnoyarsk_market_mean"
        assert estimated["autostop_price_rub"] is None
    assert estimated["labor_time_sample"]["valid_count"] > 0


def test_acquired_labor_range_survives_legacy_normalization_and_estimate(monkeypatch, forbidden_network):
    from autostop_manager import work_pricing, work_pricing_research as research

    monkeypatch.setattr(
        research,
        "_ddg_search",
        lambda query, **kwargs: {
            "results": [
                {
                    "source": f"sto-{index}.example",
                    "url": f"https://sto-{index}.example/norms",
                    "snippet": "Красноярск, замена масла 1-3 нормо-часа",
                }
                for index in range(2)
            ]
        },
    )
    acquired = labor.collect_work_price_evidence(["замена масла"], {"make": "DEMO"}, max_queries=4)
    normalized = work_pricing._normalize_labor_time_row(acquired["data"]["labor"][0])
    assert normalized["hours"] == 2 and normalized["range_hours"] == [1, 3]
    estimated = work_pricing.estimate_repair_work_cost(
        work_items=["замена масла"], make="DEMO", price_evidence=acquired
    )
    assert estimated["labor_time_range_hours"] == [1, 3]
    assert estimated["operation_estimates"][0]["labor_time_analysis"]["range_hours"] == [1, 3]
