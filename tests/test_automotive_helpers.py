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


@pytest.mark.parametrize("bounds", [[2, None], [2, 1], None, "unknown"])
def test_explicit_unknown_labor_range_never_falls_back_to_exact_hours(forbidden_network, bounds):
    row = labor.calculate_work_price(
        [{"operation_name": "demo", "hours": 1, "range_hours": bounds}],
        {"basis": "hourly_rate", "version": "1"},
        hourly_rate=1000,
    )
    assert row["data"]["total"] is None and row["missing_fields"]


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
