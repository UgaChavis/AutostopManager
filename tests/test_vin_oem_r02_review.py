"""R02 regressions: synthetic facts, inert registration and denied external I/O."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import itertools
import io
import json
import socket
import threading
import time

import pytest

from autostop_manager import automotive_identity as identity
from autostop_manager import automotive_parts as parts
from autostop_manager import oem_candidate_web_evidence as web
from autostop_manager import vehicle_identity as legacy
from autostop_manager import vehicle_identity_async as asynchronous
from autostop_manager import vehicle_identity_transport as transport
from autostop_manager.automotive_contracts import binding, result
from autostop_manager.automotive_mcp import register_automotive_tools
from autostop_manager.listing_executor import BoundedListingExecutor, read_execution_deadline
from autostop_manager.tecdoc_vehicle_selection import _independent_values

VIN = "WBA" + "0" * 14
REF = {"provider": "partsapi_ru", "namespace": "tecdoc", "entity_kind": "modification", "id": "42", "carType": "PC"}


@pytest.fixture(autouse=True)
def isolated_synthetic_state(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("R02 forbids external network and DNS")

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "disposable.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "no-runtime"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "j1-vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(legacy, "catalog_provider_status", lambda: {"providers": []})
    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())


def _source(profile):
    return result(
        "synthetic",
        "success",
        {
            "input_binding": binding(VIN),
            "vehicle_profile": profile,
            "field_statuses": dict.fromkeys(profile, "supported"),
        },
        evidence=[{"provider": "synthetic", "primary_lineage": "synthetic", "method": "fixture"}],
    )


def _primary(scope="modification"):
    return {
        "provider": "manufacturer",
        "primary_lineage": "synthetic OEM document",
        "method": "fixture",
        "locator": "https://example.invalid/document",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "declares_oem": True,
        "part_number": "DEMO-123",
        "brand": "DEMO",
        "scope": scope,
        "catalog_ref": REF,
        "identifier_binding": binding(VIN),
    }


def _native_tool(name):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("R02 isolated SDK registration")
    register_automotive_tools(server)
    return server._tool_manager.get_tool(name)


def _unwrap(value):
    return value[1] if isinstance(value, tuple) else value


@pytest.mark.parametrize("field", ["power_kw", "power_hp", "displacement_cc"])
@pytest.mark.parametrize("path", ["sync", "async"])
def test_r02_02_huge_technical_integer_is_an_isolated_invalid_row(field, path):
    items = [{"identifier": VIN}, {"identifier": VIN, field: 10**400}, {"identifier": VIN, field: 80}]
    response = (
        legacy.decode_vehicle_identities(items, live_vpic=False)
        if path == "sync"
        else asyncio.run(asynchronous.decode_vehicle_identities_async(items, live_vpic=False))
    )
    assert response["count"] == 3
    rows = response["results"]
    assert [row["item_index"] for row in rows] == [0, 1, 2]
    assert [row["ok"] for row in rows] == [True, False, True]
    assert rows[1]["vehicle_profile"] == {}
    assert rows[1]["errors"][0]["code"] == "expected_positive_finite_number"
    assert rows[1]["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False
    assert response["processing"]["http_attempts"] == 0


@pytest.mark.parametrize(
    "field,values", [("transmission", ("Automatic", "6AT", "8AT")), ("market", ("ROW", "Europe", "Japan"))]
)
@pytest.mark.parametrize("detail", ["full", "summary"])
def test_r02_04_specific_conflicts_survive_every_order_and_presentation(field, values, detail):
    for permutation in itertools.permutations(values):
        response = identity.reconcile_vehicle_identity(
            VIN, [_source({field: value}) for value in permutation], detail=detail
        )
        assert field not in response["data"]["vehicle_profile"]
        assert response["data"]["field_statuses"][field] == "disputed"
        conflict = next(row for row in response["conflicts"] if row["field"] == field)
        assert {row["value"] for row in conflict["alternatives"]} == set(values)
        assert [row["profile"][field] for row in response["data"]["variants"]] == list(permutation)
        reused = identity.reconcile_vehicle_identity(VIN, [response])
        assert reused["data"]["field_statuses"][field] == "disputed"
        assert field not in reused["data"]["vehicle_profile"]
        assert any(row["field"] == field for row in reused["conflicts"])


@pytest.mark.parametrize("detail", ["full", "summary"])
def test_r02_05_gear_count_and_conflict_are_reusable_typed_facts(detail):
    first = _source({"transmission": "Automatic", "transmission_speeds": 6})
    response = identity.reconcile_vehicle_identity(VIN, [first], detail=detail)
    assert response["data"]["vehicle_profile"]["transmission_speeds"] == 6
    assert response["data"]["provenance"]["transmission_speeds"][0]["primary_lineage"] == "synthetic"
    reused = identity.reconcile_vehicle_identity(VIN, [response])
    assert reused["data"]["vehicle_profile"]["transmission_speeds"] == 6
    disputed = identity.reconcile_vehicle_identity(VIN, [first, _source({"transmission_speeds": 8})], detail=detail)
    assert disputed["data"]["field_statuses"]["transmission_speeds"] == "disputed"
    assert "transmission_speeds" not in disputed["data"]["vehicle_profile"]
    assert any(row["field"] == "transmission_speeds" for row in disputed["conflicts"])
    assert disputed["data"]["parts_lookup_readiness"]["exact_applicability_confirmed"] is False


def test_r02_05_main_e2_vpic_numeric_string_gear_count_survives_reconciliation():
    ready = legacy.decode_vehicle_identity(
        VIN,
        live_vpic=False,
        live_wmi=False,
        vpic_result={
            "ok": True,
            "vehicle": {"vin": VIN, "make": "BMW", "model": "DEMO", "transmissionspeeds": "6"},
            "error_code": "0",
            "identifier_binding": {"status": "exact", "verified": True},
        },
    )
    response = identity.reconcile_vehicle_identity(VIN, [ready])
    assert response["data"]["vehicle_profile"]["transmission_speeds"] == 6
    assert response["data"]["field_statuses"]["transmission_speeds"] == "candidate"
    assert response["data"]["trust"]["state"] == "caller_supplied_unverified"
    assert response["data"]["provenance"]["transmission_speeds"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_year", False),
        ("make", True),
        ("engine", {"malformed": "E1"}),
        ("engine", ["E1", "E2"]),
        ("options", "A,B"),
        ("options", ["A", 1]),
        ("power_hp", -5),
        ("power_kw", 10**400),
        ("production_date", "2015-02-30"),
        ("transmission_speeds", 0),
        ("manufacturer", {}),
    ],
)
@pytest.mark.parametrize("detail", ["full", "summary"])
def test_r02_06_malformed_supplied_fields_never_become_canonical_supported(field, value, detail):
    supplied = _source({"model": "DEMO", field: value})
    before = deepcopy(supplied)
    response = identity.reconcile_vehicle_identity(VIN, [supplied], detail=detail)
    assert response["outcome"] == "partial"
    assert field not in response["data"]["vehicle_profile"]
    assert response["data"]["field_statuses"].get(field) != "supported"
    assert any(row["field"] == field and row["code"] == "invalid_vehicle_field" for row in response["conflicts"])
    assert response["data"]["vehicle_profile"].get("model") == "DEMO"
    assert response["data"]["variants"][0]["profile"][field] == value
    assert supplied == before
    assert response["execution"]["network_calls"] == 0


def test_r02_06_supported_options_years_and_numeric_values_remain_usable():
    profile = {
        "model_year": "2015",
        "production_year": 2014,
        "production_date": "2014-12",
        "engine": "E1",
        "options": ["A", " B "],
        "power_hp": 150,
        "power_kw": 100.5,
        "transmission_speeds": 6,
    }
    response = identity.reconcile_vehicle_identity(VIN, [_source(profile)], detail="summary")
    actual = response["data"]["vehicle_profile"]
    assert actual == {**profile, "model_year": 2015, "options": ["A", "B"]}
    assert {row["field"] for row in response["data"]["variants"][0]["normalization_notes"]} == {"model_year"}
    assert all(status == "candidate" for status in response["data"]["field_statuses"].values())
    assert response["data"]["trust"]["state"] == "caller_supplied_unverified"


def _vpic_payload(error="0", returned=None):
    return {
        "Results": [
            {
                "VIN": returned or VIN,
                "Make": "BMW",
                "Model": "DEMO",
                "ModelYear": "2015",
                "EngineModel": "DEMO-E1",
                "TransmissionStyle": "Automatic",
                "TransmissionSpeeds": "6",
                "DriveType": "4WD",
                "DisplacementCC": "1998",
                "EngineHP": "150",
                "ErrorCode": error,
                "ErrorText": "",
            }
        ]
    }


def test_r02_07_real_vpic_parser_projects_reusable_fields_and_redacts_identifier(monkeypatch):
    calls = []
    monkeypatch.setattr(
        identity.vin_lookup, "_vpic_request_json", lambda *args, **kwargs: calls.append(1) or _vpic_payload()
    )
    response = identity.decode_vin_vpic(VIN)
    profile = response["data"]["vehicle_profile"]
    expected = {
        "make": "BMW",
        "model": "DEMO",
        "model_year": 2015,
        "engine": "DEMO-E1",
        "transmission": "Automatic",
        "transmission_speeds": 6,
        "drivetrain": "4WD",
        "displacement_cc": 1998,
        "power_hp": 150,
    }
    assert profile == expected
    assert VIN not in json.dumps(response)
    assert response["data"]["input_binding"] == binding(VIN)
    assert response["data"]["identifier_binding"] == {"status": "exact", "verified": True}
    assert calls == [1]
    for detail in ("full", "summary"):
        reconciled = identity.reconcile_vehicle_identity(VIN, [response], detail=detail)
        assert reconciled["data"]["vehicle_profile"] == expected
        assert {
            origin["primary_lineage"] for origins in reconciled["data"]["provenance"].values() for origin in origins
        } == {"nhtsa_vpic"}
    foreign = identity.reconcile_vehicle_identity("WBA" + "1" * 14, [response])
    assert not foreign["data"]["vehicle_profile"]
    assert foreign["conflicts"][0]["code"] == "result_binding_mismatch"


def test_r02_07_nonclean_binding_and_year_hint_are_not_upgraded(monkeypatch):
    monkeypatch.setattr(identity.vin_lookup, "_vpic_request_json", lambda *args, **kwargs: _vpic_payload("7"))
    partial = identity.decode_vin_vpic(VIN)
    for field in (
        "model_year",
        "engine",
        "transmission",
        "transmission_speeds",
        "drivetrain",
        "power_hp",
        "displacement_cc",
    ):
        assert field not in partial["data"]["vehicle_profile"]
    assert partial["data"]["diagnostics"]["error_codes"] == ["7"]
    monkeypatch.setattr(identity.vin_lookup, "_vpic_request_json", lambda *args, **kwargs: _vpic_payload())
    hinted = identity.decode_vin_vpic(VIN, model_year=2015)
    assert hinted["data"]["field_statuses"]["model_year"] == "candidate"
    merged = identity.reconcile_vehicle_identity(VIN, [hinted])
    assert merged["data"]["field_statuses"]["model_year"] == "candidate"
    assert all(origin["independent"] is False for origin in merged["data"]["provenance"]["model_year"])
    monkeypatch.setattr(
        identity.vin_lookup, "_vpic_request_json", lambda *args, **kwargs: _vpic_payload(returned="WBA" + "1" * 14)
    )
    wrong = identity.decode_vin_vpic(VIN)
    assert wrong["outcome"] == "partial"
    assert not wrong["data"]["vehicle_profile"]
    assert VIN not in json.dumps(wrong)


@pytest.mark.parametrize("low_volume", [False, True])
@pytest.mark.parametrize("formatted", [False, True])
def test_r02_08_correct_wmi_is_only_a_manufacturer_family_fact(low_volume, formatted):
    identifier = "AA9" + "A" * 8 + "XYZ" + "001" if low_volume else VIN
    wmi = transport.wmi_for_vin(identifier)
    row = result(
        "decode_wmi_vpic",
        "success",
        {
            "wmi": wmi,
            "input_binding": None,
            "identifier_binding": {"status": "exact", "verified": True},
            "vehicle_profile": {"manufacturer": "Synthetic maker", "engine": "discard engine", "model_year": 2015},
            "field_statuses": {"manufacturer": "supported"},
        },
    )
    supplied = " " + identifier.lower() + " " if formatted else identifier
    response = identity.reconcile_vehicle_identity(supplied, [row])
    assert response["data"]["vehicle_profile"] == {"manufacturer": "Synthetic maker"}
    assert response["data"]["field_statuses"]["manufacturer"] == "candidate"
    assert not response["conflicts"]
    wrong = identity.reconcile_vehicle_identity(supplied, [{**row, "data": {**row["data"], "wmi": wmi[:-1] + "0"}}])
    assert wrong["conflicts"][0]["code"] == "result_binding_mismatch"


@pytest.mark.parametrize(
    "field,broad,specific,other", [("transmission", "Automatic", "6AT", "8AT"), ("market", "ROW", "Europe", "Japan")]
)
@pytest.mark.parametrize("condition_source", ["criteria", "evidence"])
@pytest.mark.parametrize("as_list", [False, True])
def test_r02_09_fitment_requires_specificity_in_the_direction_of_the_condition(
    field, broad, specific, other, condition_source, as_list
):
    source = _primary()
    required = [specific] if as_list else specific
    criteria = {field: required} if condition_source == "criteria" else {}
    if condition_source == "evidence":
        source["conditions"] = {field: required}
    for actual, state in ((broad, "unknown"), (specific, "supported"), (other, "rejected")):
        response = parts.assess_part_fitment(
            {"vehicle_profile": {field: actual}, "catalog_ref": REF},
            {"number": "DEMO-123", "brand": "DEMO"},
            criteria,
            [source],
        )
        assert response["data"]["state"] == state
        assert response["data"]["checked_conditions"][0]["state"] == state
        assert (field in response["missing_fields"]) is (state == "unknown")
    assert parts._criterion(field, broad, specific) == "supported"
    assert legacy.identity_values_agree(field, broad, specific) is True


def test_r02_09_known_gear_count_can_refine_a_type_but_a_broad_model_cannot_refine_itself():
    assert parts._criterion("model", "3 Series F30", "3 Series") == "unknown"
    assert parts._criterion("model", "3 Series", "3 Series F30") == "supported"
    vehicle = {"vehicle_profile": {"transmission": "Automatic", "transmission_speeds": 6}, "catalog_ref": REF}
    response = parts.assess_part_fitment(
        vehicle, {"number": "DEMO-123", "brand": "DEMO"}, {"transmission": "6AT"}, [_primary()]
    )
    assert response["data"]["state"] == "supported"
    vehicle["vehicle_profile"]["transmission_speeds"] = 8
    response = parts.assess_part_fitment(
        vehicle, {"number": "DEMO-123", "brand": "DEMO"}, {"transmission": "6AT"}, [_primary()]
    )
    assert response["data"]["state"] == "rejected"


@pytest.mark.parametrize(
    "field,actual,lower,upper",
    [("production_year", 2015, 2010, 2020), ("production_date", "2015-06-15", "2010-01-01", "2020-12-31")],
)
@pytest.mark.parametrize("condition_source", ["criteria", "evidence"])
def test_r02_10_unknown_range_keys_are_unknown_without_implementing_new_operators(
    field, actual, lower, upper, condition_source
):
    source = _primary("exact_identifier")
    expected = {"from": lower, "to": upper, "none_of": [actual]}
    criteria = {field: expected} if condition_source == "criteria" else {}
    if condition_source == "evidence":
        source["conditions"] = {field: expected}
    response = parts.assess_part_fitment(
        {"vehicle_profile": {field: actual}, "input_binding": binding(VIN)},
        {"number": "DEMO-123", "brand": "DEMO"},
        criteria,
        [source],
        "exact_identifier",
    )
    assert response["data"]["state"] == "unknown"
    assert response["missing_fields"] == [field]
    assert parts._criterion(field, {"from": lower}, actual) == "supported"
    assert parts._criterion(field, {"to": upper}, actual) == "supported"
    assert parts._criterion(field, {"from": lower, "to": upper}, actual) == "supported"


@pytest.mark.parametrize(
    "title,snippet,path,found",
    [
        ("DEMO-12345", "", "DEMO-12345", False),
        ("XDEMO-123", "", "XDEMO-123", False),
        ("X-DEMO-123", "", "X-DEMO-123", False),
        ("DEMO-123-45", "", "DEMO-123-45", False),
        ("DEMO", "123", "unrelated", False),
        ("DE MO 123", "", "unrelated", False),
        ("DEMO-123", "", "unrelated", True),
        ("DEMO 123", "", "unrelated", True),
        ("DEMO.123", "", "unrelated", True),
        ("DEMO/123", "", "unrelated", True),
        ("DEM OIL", "", "DEMO-123", True),
        ("Part DEMO-123 for test", "", "unrelated", True),
    ],
)
def test_r02_11_web_references_match_the_whole_number_without_field_or_word_fusion(
    monkeypatch, title, snippet, path, found
):
    url = "https://exist.ru/parts/" + path
    monkeypatch.setattr(
        web,
        "research_part_public_evidence",
        lambda **kwargs: {
            "ok": True,
            "results": [
                {
                    "url": url,
                    "title": title,
                    "snippet": snippet,
                    "source_authorized": True,
                    "source_id": "exist",
                    "source_type": "price_catalog",
                    "domain": "exist.ru",
                }
            ],
        },
    )
    response = web.verify_oem_candidates_web(candidates=[{"part_number": "DEMO-123"}], live_search=True)
    candidate = response["candidate_evidence"][0]
    assert candidate["evidence"][0]["part_number_reference_found"] is found
    assert candidate["evidence"][0]["url"] == url
    assert candidate["fitment_confirmed"] is False


@pytest.mark.parametrize(
    "carrier", ["root-provider", "nested-provider", "compact-provider", "field-provenance", "legacy-field-provenance"]
)
@pytest.mark.parametrize("path", ["sync-single", "sync-batch", "async-single", "async-batch"])
def test_r02_12_carried_provider_lineage_never_becomes_independent_crm(path, carrier):
    profile = {"make": "BMW", "model": "DEMO", "engine": "DEMO-E1"}
    if carrier == "root-provider":
        context = {"provider": "partsapi_ru", "vehicle_profile": profile}
    elif carrier == "nested-provider":
        context = {"vehicle_profile": {**profile, "primary_lineage": "partsapi_ru"}}
    elif carrier == "compact-provider":
        context = {"vehicle_profile_compact": json.dumps({**profile, "provider": "partsapi_ru"})}
    elif carrier == "field-provenance":
        context = {
            **profile,
            "provenance": {"engine": [{"value": profile["engine"], "primary_lineage": "partsapi_ru"}]},
        }
    else:
        context = {**profile, "field_provenance": {"engine": "partsapi_ru"}}
    before = deepcopy(context)
    if path == "sync-single":
        response = legacy.decode_vehicle_identity(VIN, crm_context=context, live_vpic=False, live_wmi=False)
    elif path == "sync-batch":
        response = legacy.decode_vehicle_identities([{"identifier": VIN, "crm_context": context}], live_vpic=False)[
            "results"
        ][0]
    elif path == "async-single":
        response = asyncio.run(
            asynchronous.decode_vehicle_identity_async(VIN, crm_context=context, live_vpic=False, live_wmi=False)
        )
    else:
        response = asyncio.run(
            asynchronous.decode_vehicle_identities_async([{"identifier": VIN, "crm_context": context}], live_vpic=False)
        )["results"][0]
    assert response["provenance"]["engine"][0]["source"] == "partsapi_ru"
    assert _independent_values(response, {})["engine"] == []
    assert context == before


def test_r02_12_explicit_client_facts_keep_their_existing_independence():
    response = asyncio.run(
        asynchronous.decode_vehicle_identities_async([{"identifier": VIN, "engine": "DEMO-E1"}], live_vpic=False)
    )["results"][0]
    assert _independent_values(response, {})["engine"] == ["DEMO-E1"]


@pytest.mark.parametrize("value", [["DEMO123"], ["DEMO", "123"], {"DEMO": "123"}])
@pytest.mark.parametrize("route", ["capture", "fitment"])
def test_r02_13_a_container_cannot_bind_one_source_part_number(value, route):
    source = {**_primary("family"), "part_number": value}
    if route == "capture":
        response = parts.capture_oem_evidence("DEMO123", source, "family", brand="DEMO")
        assert response["data"]["candidate"]["oem_confirmed"] is False
        assert "source_part_number" in response["missing_fields"]
    else:
        response = parts.assess_part_fitment({}, {"number": "DEMO123", "brand": "DEMO"}, {}, [source], "family")
        assert response["data"]["state"] == "unknown"
        assert "primary_bound_fitment_evidence" in response["missing_fields"]
    assert response["execution"]["network_calls"] == 0


def test_r02_13_existing_numeric_scalar_number_policy_is_preserved():
    source = {**_primary("family"), "part_number": 1234}
    response = parts.capture_oem_evidence("1234", source, "family", brand="DEMO")
    assert response["data"]["candidate"]["oem_confirmed"] is True


async def _until(predicate):
    expires = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < expires, "synthetic setup timed out"
        await asyncio.sleep(0.001)


def test_r02_01_native_queued_rows_expire_without_starting_after_real_queue_drains(monkeypatch):
    calls, release, entered = [], threading.Event(), threading.Event()
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)
    monkeypatch.setattr(
        identity, "decode_vin_vpic", lambda value, **kwargs: calls.append(value) or result("synthetic", "success", {})
    )

    async def run():
        loop = asyncio.get_running_loop()
        tool = _native_tool("decode_vehicle_batch")

        def block():
            entered.set()
            assert release.wait(2)
            return {"ok": True}

        blocker = asyncio.create_task(pool.run(block))
        await _until(entered.is_set)
        tasks = [
            asyncio.create_task(
                tool.run(
                    {"items": ["SYNTHETIC_ROW"], "decoder": "decode_vin_vpic", "deadline_seconds": 0.2},
                    convert_result=True,
                )
            )
            for _ in range(6)
        ]
        try:
            await asyncio.sleep(0.3)
            finished_before_release = all(task.done() for task in tasks)
            assert calls == []
        finally:
            release.set()
            responses = [_unwrap(response) for response in await asyncio.gather(*tasks)]
            await blocker
            await loop.run_in_executor(None, lambda: None)
        assert finished_before_release
        assert calls == []
        outcomes = [response["data"]["items"][0]["result"]["outcome"] for response in responses]
        assert outcomes.count("deadline_exceeded") == 1
        assert outcomes.count("provider_busy") == 5
        assert all(response["execution"]["network_calls"] == 0 for response in responses)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("running", [False, True])
def test_r02_01_cancellation_stops_admission_and_preserves_the_running_worker(monkeypatch, running):
    calls, entered, release = [], threading.Event(), threading.Event()
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=int(not running), timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)

    def decode(identifier, **kwargs):
        calls.append(identifier)
        entered.set()
        assert release.wait(2)
        return result("synthetic", "success", {})

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)

    async def run():
        loop = asyncio.get_running_loop()
        held = None
        if not running:

            def hold():
                entered.set()
                assert release.wait(2)
                return {"ok": True}

            held = asyncio.create_task(pool.run(hold))
            await _until(entered.is_set)
        task = asyncio.create_task(identity.decode_vehicle_batch_async(["FIRST", "SECOND"], "decode_vin_vpic", 1))
        try:
            if running:
                await _until(entered.is_set)
            else:
                await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.pending_count == (1 if running else 2)
            refused = await identity.decode_vehicle_batch_async(["EXTRA"], "decode_vin_vpic", 1)
            assert refused["data"]["items"][0]["result"]["outcome"] == "provider_busy"
            assert await asyncio.wait_for(loop.run_in_executor(None, lambda: True), 0.2)
            assert calls == (["FIRST"] if running else [])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if held is not None:
                await held
            await _until(lambda: pool.pending_count == 0)
        assert calls == (["FIRST"] if running else [])

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


def test_r02_01_running_timeout_retains_worker_and_marks_network_count_unknown(monkeypatch):
    entered, release, calls = threading.Event(), threading.Event(), []
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)

    def decode(identifier, **kwargs):
        calls.append(identifier)
        entered.set()
        assert release.wait(2)
        return result("synthetic", "success", {}, network_calls=1)

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(identity.decode_vehicle_batch_async(["FIRST", "SECOND"], "decode_vin_vpic", 0.2))
        try:
            await _until(entered.is_set)
            response = await asyncio.wait_for(asyncio.shield(task), 0.8)
            assert [row["result"]["outcome"] for row in response["data"]["items"]] == ["deadline_exceeded"] * 2
            assert response["execution"]["network_calls"] is None
            assert response["execution"]["network_calls_lower_bound"] == 0
            assert response["data"]["items"][0]["result"]["execution"]["decoder_started"] is True
            assert pool.pending_count == 1
            assert await asyncio.wait_for(loop.run_in_executor(None, lambda: True), 0.2)
            refused = await identity.decode_vehicle_batch_async(["EXTRA"], "decode_vin_vpic", 1)
            assert refused["data"]["items"][0]["result"]["outcome"] == "provider_busy"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await _until(lambda: pool.pending_count == 0)
        assert calls == ["FIRST"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


def test_r02_03_subminimum_remaining_budget_keeps_completed_rows_and_known_counts(monkeypatch):
    calls = []

    def decode(identifier, **kwargs):
        calls.append(identifier)
        time.sleep(0.13)
        return result(
            "synthetic", "success", {}, network_calls=1, attempts=[{"method": "fixture", "outcome": "success"}]
        )

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)
    response = asyncio.run(identity.decode_vehicle_batch_async(["FIRST", "SECOND"], "decode_vin_vpic", 0.2))
    assert [row["result"]["outcome"] for row in response["data"]["items"]] == ["success", "deadline_exceeded"]
    assert response["execution"]["network_calls"] == 1
    assert len(response["execution"]["attempts"]) == 1
    assert calls == ["FIRST"]


def test_r02_03_minimum_valid_budget_and_positive_remaining_do_not_invalidate_native_batch():
    tool = _native_tool("decode_vehicle_batch")
    response = _unwrap(
        asyncio.run(
            tool.run(
                {"items": ["WBA", "WBA"], "decoder": "decode_wmi_local", "deadline_seconds": 0.1}, convert_result=True
            )
        )
    )
    assert response["outcome"] != "invalid_input"
    assert len(response["data"]["items"]) == 2
    assert response["execution"]["network_calls"] == 0


def test_r02_native_supplied_nested_types_and_range_conditions_follow_domain_guards():
    reconcile = _native_tool("reconcile_vehicle_identity")
    response = _unwrap(
        asyncio.run(
            reconcile.run({"identifier": VIN, "results": [_source({"engine": {"invalid": "E1"}})]}, convert_result=True)
        )
    )
    assert "engine" not in response["data"]["vehicle_profile"]
    fitment = _native_tool("assess_part_fitment")
    response = _unwrap(
        asyncio.run(
            fitment.run(
                {
                    "vehicle": {"vehicle_profile": {"production_year": 2015}, "input_binding": binding(VIN)},
                    "part": {"number": "DEMO-123", "brand": "DEMO"},
                    "criteria": {"production_year": {"from": 2010, "none_of": [2015]}},
                    "evidence": [_primary("exact_identifier")],
                    "scope": "exact_identifier",
                },
                convert_result=True,
            )
        )
    )
    assert response["data"]["state"] == "unknown"


def _legacy_native_batch(monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from autostop_manager import mcp_tools

    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    for name in (
        "StoreState",
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, Inert)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    server = FastMCP("R02 inert E2 registration")
    mcp_tools.register_manager_tools(
        server, store=Inert(), store_client=Inert(), include_tools={"decode_vehicle_identities"}
    )
    return server._tool_manager.get_tool("decode_vehicle_identities")


def _context_identity_path(path, context, monkeypatch):
    if path == "sync-single":
        return legacy.decode_vehicle_identity(VIN, crm_context=context, live_vpic=False, live_wmi=False)
    if path == "sync-batch":
        return legacy.decode_vehicle_identities([{"identifier": VIN, "crm_context": context}], live_vpic=False)[
            "results"
        ][0]
    if path == "async-single":
        return asyncio.run(
            asynchronous.decode_vehicle_identity_async(VIN, crm_context=context, live_vpic=False, live_wmi=False)
        )
    if path == "async-batch":
        return asyncio.run(
            asynchronous.decode_vehicle_identities_async([{"identifier": VIN, "crm_context": context}], live_vpic=False)
        )["results"][0]
    if path == "builder":
        from autostop_manager.vehicle_identity_inputs import validate_identity_input

        return asynchronous._build_row(validate_identity_input(VIN, context), None, None)
    tool = _legacy_native_batch(monkeypatch)
    return _unwrap(
        asyncio.run(
            tool.run({"items": [{"identifier": VIN, "crm_context": context}], "live_vpic": False}, convert_result=True)
        )
    )["results"][0]


@pytest.mark.parametrize(
    "path", ["sync-single", "sync-batch", "async-single", "async-batch", "builder", "native-batch"]
)
@pytest.mark.parametrize("carrier", ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"])
@pytest.mark.parametrize("empty", [None, "", " \t "])
def test_r02_12_empty_outer_field_retains_the_actual_nested_provider_origin(path, carrier, empty, monkeypatch):
    nested = {"provider": "partsapi_ru", "make": "DEMO", "model": "DEMO", "engine": "DEMO-E1"}
    context = {carrier: json.dumps(nested) if carrier == "vehicle_profile_compact" else nested, "engine": empty}
    before = deepcopy(context)
    response = _context_identity_path(path, context, monkeypatch)
    assert response["vehicle_profile"]["engine"] == "DEMO-E1"
    assert response["provenance"]["engine"][0]["source"] == "partsapi_ru"
    assert _independent_values(response, {})["engine"] == []
    assert context == before


@pytest.mark.parametrize(
    "path", ["sync-single", "sync-batch", "async-single", "async-batch", "builder", "native-batch"]
)
@pytest.mark.parametrize("carrier", ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"])
def test_r02_12_nonempty_outer_override_remains_a_real_explicit_fact(path, carrier, monkeypatch):
    nested = {"provider": "partsapi_ru", "make": "DEMO", "model": "DEMO", "engine": "DEMO-E1"}
    context = {carrier: json.dumps(nested) if carrier == "vehicle_profile_compact" else nested, "engine": "DEMO-E2"}
    response = _context_identity_path(path, context, monkeypatch)
    assert response["vehicle_profile"]["engine"] == "DEMO-E2"
    assert response["provenance"]["engine"][0]["source"] == "CRM context"
    assert _independent_values(response, {})["engine"] == ["DEMO-E2"]


@pytest.mark.parametrize("low_volume", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_r02_08_actual_wmi_endpoint_roundtrip_retains_the_public_family_key(low_volume, native, monkeypatch):
    identifier = "AA9" + "A" * 8 + "XYZ" + "001" if low_volume else VIN
    wmi = transport.wmi_for_vin(identifier)
    payload = {
        "SearchCriteria": "WMI: " + wmi,
        "Results": [
            {
                "WMI": wmi,
                "Name": "Synthetic maker",
                "Make": "DEMO",
                "Country": "Synthetic country",
                "EngineModel": "discard engine",
            }
        ],
    }
    calls = []

    def fake_bytes(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(identity.vin_lookup, "urlopen", fake_bytes)
    if native:
        tool = _native_tool("decode_wmi_vpic")
        decoded = _unwrap(asyncio.run(tool.run({"wmi": wmi}, convert_result=True)))
    else:
        decoded = identity.decode_wmi_vpic(wmi)
    assert calls == [1]
    assert decoded["data"]["wmi"] == wmi
    assert decoded["data"]["input_binding"] is None
    assert decoded["data"]["identifier_binding"] == {"status": "exact", "verified": True}
    response = identity.reconcile_vehicle_identity(identifier, [decoded])
    assert response["data"]["vehicle_profile"]["manufacturer"] == "Synthetic maker"
    assert all(level == "candidate" for level in response["data"]["field_statuses"].values())
    assert "engine" not in response["data"]["vehicle_profile"]
    assert not response["conflicts"]


@pytest.mark.parametrize("value,requested", [(True, "TRUE"), (False, "FALSE")])
@pytest.mark.parametrize("route", ["capture", "fitment"])
def test_r02_13_a_boolean_never_binds_a_source_part_number(value, requested, route):
    source = {**_primary("family"), "part_number": value}
    if route == "capture":
        response = parts.capture_oem_evidence(requested, source, "family", brand="DEMO")
        assert response["data"]["candidate"]["oem_confirmed"] is False
        assert "source_part_number" in response["missing_fields"]
    else:
        response = parts.assess_part_fitment({}, {"number": requested, "brand": "DEMO"}, {}, [source], "family")
        assert response["data"]["state"] == "unknown"
    assert response["execution"]["network_calls"] == 0


def test_r02_01_native_capacity_is_two_workers_two_waiters_without_cancelled_tombstones(monkeypatch):
    pool = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)
    release = threading.Event()
    lock, active, peak, calls = threading.Lock(), 0, 0, []

    def decode(identifier, **kwargs):
        nonlocal active, peak
        with lock:
            calls.append(identifier)
            active += 1
            peak = max(active, peak)
        try:
            assert release.wait(3)
            return result("synthetic", "success", {})
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)
    tool = _native_tool("decode_vehicle_batch")

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        tasks = [
            asyncio.create_task(
                tool.run(
                    {"items": ["NO_INPUT_ECHO"], "decoder": "decode_vin_vpic", "deadline_seconds": 1},
                    convert_result=True,
                )
            )
            for _ in range(12)
        ]
        try:
            await _until(lambda: pool.pending_count == 4 and active == 2)
            await asyncio.sleep(0.02)
            assert peak == 2 and len(calls) == 2
            assert pool._pool._work_queue.qsize() == 2
            assert sum(task.done() for task in tasks) == 8
            assert await asyncio.wait_for(loop.run_in_executor(None, lambda: True), 0.2)
            for task in tasks:
                task.cancel()
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            assert sum(isinstance(response, asyncio.CancelledError) for response in responses) == 4
            assert pool.pending_count == 4 and pool._pool._work_queue.qsize() == 2
            extra = [
                _unwrap(
                    await tool.run(
                        {"items": ["NO_INPUT_ECHO"], "decoder": "decode_vin_vpic", "deadline_seconds": 1},
                        convert_result=True,
                    )
                )
                for _ in range(8)
            ]
            for response in extra:
                row = response["data"]["items"][0]["result"]
                assert row["outcome"] == "provider_busy"
                assert row["adapter_started"] is False and row["retry_attempted"] is False
                assert row["execution"]["network_calls"] == 0
                assert "NO_INPUT_ECHO" not in json.dumps(response)
            assert pool.pending_count == 4 and pool._pool._work_queue.qsize() == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _until(lambda: pool.pending_count == 0)
        assert len(calls) == 2 and active == 0
        assert pool._pool._work_queue.qsize() == 0

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()
    assert not any(thread.is_alive() for thread in pool._pool._threads)


def test_r02_01_queued_worker_receives_the_original_context_deadline(monkeypatch):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)
    entered, release, observed = threading.Event(), threading.Event(), []

    def hold():
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    def decode(identifier, **kwargs):
        observed.append((read_execution_deadline.get(), kwargs["timeout_seconds"]))
        return result("synthetic", "success", {}, network_calls=1)

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)

    async def run():
        blocker = asyncio.create_task(pool.run(hold))
        await _until(entered.is_set)
        expires = time.monotonic() + 0.35
        token = read_execution_deadline.set(expires)
        task = asyncio.create_task(identity.decode_vehicle_batch_async(["SYNTHETIC"], "decode_vin_vpic", 1))
        try:
            await _until(lambda: pool.pending_count == 2)
            await asyncio.sleep(0.12)
            release.set()
            response = await task
            assert response["outcome"] == "success"
            assert observed[0][0] == expires
            assert 0.1 <= observed[0][1] < 0.25
            assert read_execution_deadline.get() == expires
        finally:
            read_execution_deadline.reset(token)
            release.set()
            await asyncio.gather(task, blocker, return_exceptions=True)
            await _until(lambda: pool.pending_count == 0)
        assert read_execution_deadline.get() is None

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


def test_r02_01_unavailable_local_executor_returns_sanitized_refusal(monkeypatch):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    pool.close()
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)
    response = asyncio.run(identity.decode_vehicle_batch_async(["NO_INPUT_ECHO"], "decode_vin_vpic", 1))
    row = response["data"]["items"][0]["result"]
    assert row["outcome"] == "provider_executor_unavailable"
    assert row["stage"] == "admission" and row["adapter_started"] is False
    assert row["retry_attempted"] is False and row["retryable"] is False
    assert row["execution"] == {"network_calls": 0, "attempts": []}
    assert "NO_INPUT_ECHO" not in json.dumps(response)
    assert pool.pending_count == 0


def test_r02_01_known_counts_survive_an_unknown_later_running_read(monkeypatch):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", pool, raising=False)
    release = threading.Event()
    calls = []

    def decode(identifier, **kwargs):
        calls.append(identifier)
        if identifier == "SECOND":
            assert release.wait(3)
        return result("synthetic", "success", {}, network_calls=1)

    monkeypatch.setattr(identity, "decode_vin_vpic", decode)

    async def run():
        try:
            response = await identity.decode_vehicle_batch_async(["FIRST", "SECOND", "THIRD"], "decode_vin_vpic", 0.2)
            assert response["execution"]["network_calls"] is None
            assert response["execution"]["network_calls_lower_bound"] == 1
            assert [row["result"]["outcome"] for row in response["data"]["items"]] == [
                "success",
                "deadline_exceeded",
                "deadline_exceeded",
            ]
            assert pool.pending_count == 1
        finally:
            release.set()
            await _until(lambda: pool.pending_count == 0)
        assert calls == ["FIRST", "SECOND"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("low_volume", [False, True])
@pytest.mark.parametrize("case", ["bound", "missing", "mismatch", "malformed", "multiple_rows"])
def test_r02_v3_native_wmi_keeps_nullable_family_shape_without_promoting_unknown_facts(low_volume, case, monkeypatch):
    identifier = "AA9" + "A" * 8 + "XYZ" + "001" if low_volume else VIN
    wmi = transport.wmi_for_vin(identifier)
    raw = {
        "ManufacturerName": "Synthetic maker",
        "Make": "DEMO",
        "VehicleType": "Passenger Car",
        "Model": "PRIVATE_UNCONFIRMED_VARIANT",
        "EngineModel": "PRIVATE_UNCONFIRMED_VARIANT",
    }
    payload = {"SearchCriteria": "WMI:" + wmi, "Results": [raw]}
    if case == "missing":
        payload.pop("SearchCriteria")
    elif case == "mismatch":
        payload["SearchCriteria"] = "WMI:ZZZ"
    elif case == "malformed":
        raw["ManufacturerName"] = {"bad": "PRIVATE_UNCONFIRMED_VARIANT"}
    elif case == "multiple_rows":
        payload["Results"] *= 2
    calls = []

    def fake_bytes(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(identity.vin_lookup, "urlopen", fake_bytes)
    tool = _native_tool("decode_wmi_vpic")
    content, response = asyncio.run(tool.run({"wmi": wmi}, convert_result=True))
    assert json.loads(content[0].text) == response
    assert calls == [1] and response["execution"]["network_calls"] == 1
    data = response["data"]
    assert data["wmi"] == wmi and data["input_binding"] is None
    assert set(data["vehicle_profile"]) == {"manufacturer", "make", "country", "vehicle_type"}
    assert data["vehicle_profile"]["country"] is None
    assert "PRIVATE_UNCONFIRMED_VARIANT" not in json.dumps(response)
    assert response["evidence"][0]["identifier_binding"] == data["identifier_binding"]
    reconciled = identity.reconcile_vehicle_identity(identifier, [response])
    canonical = reconciled["data"]
    assert "country" not in canonical["vehicle_profile"]
    assert "country" not in canonical["field_statuses"]
    if case == "bound":
        assert response["outcome"] == "success"
        assert data["identifier_binding"] == {"status": "exact", "verified": True}
        assert canonical["vehicle_profile"]["manufacturer"] == "Synthetic maker"
        assert all(status == "candidate" for status in canonical["field_statuses"].values())
    else:
        assert response["outcome"] in {"partial", "parse_error"}
        assert all(value is None for value in data["vehicle_profile"].values())
        assert not canonical["vehicle_profile"]
        assert not canonical["field_statuses"]
        assert reconciled["conflicts"]
    assert not canonical["parts_lookup_readiness"]["exact_applicability_confirmed"]
