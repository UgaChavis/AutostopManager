"""Native producer/consumer regressions with synthetic transport and local state."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import itertools
import socket
import subprocess
from urllib.error import HTTPError, URLError

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import automotive_identity as identity
from autostop_manager import automotive_offline as offline
from autostop_manager import vehicle_identity, vin_lookup
from autostop_manager.automotive_contracts import binding, ready_identity
from autostop_manager.listing_executor import ListingExecutionError, read_execution_deadline
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState

VIN = "WBA" + "0" * 14
CONTEXT = {"make": "BMW", "model": "Demo"}
AGGREGATES = ("generation", "transmission_code", "transfer_case", "front_differential", "rear_differential")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network, provider or subprocess forbidden")

    for attribute in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, attribute, forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(vehicle_identity, "catalog_provider_status", lambda: {"providers": []})
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "unused.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "missing-runtime"))


@pytest.fixture
def native(tmp_path):
    server = FastMCP("isolated-native-review-regressions")
    register_manager_tools(
        server,
        StoreState(tmp_path / "synthetic.sqlite3"),
        include_tools={
            "decode_vehicle_identity",
            "decode_vin_vpic",
            "decode_wmi_vpic",
            "decode_wmi_local",
            "vininfo_decode",
            "corgi_decode",
            "reconcile_vehicle_identity",
            "compare_vehicle_modifications",
            "decode_vehicle_batch",
        },
    )

    def run(name, **arguments):
        return asyncio.run(server._tool_manager.get_tool(name).run(arguments))

    return run


def assert_reusable(native, failed, *, identifier=VIN, context=None):
    merged = native("reconcile_vehicle_identity", identifier=identifier, results=[failed], context=context or CONTEXT)
    accepted, errors = ready_identity(merged, identifier)
    assert errors == [] and accepted is not None
    assert merged["conflicts"] == []
    assert merged["data"]["source_failures"] == [{"result_index": 0, "outcome": failed["outcome"]}]
    assert accepted["trust"]["state"] == "caller_supplied_unverified"
    assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
    return merged


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (TimeoutError(), "timeout"),
        (URLError("synthetic network"), "network_error"),
        (OSError("synthetic network"), "network_error"),
        (HTTPError("https://synthetic.invalid", 429, "quota", {}, None), "provider_throttled"),
        (HTTPError("https://synthetic.invalid", 503, "down", {}, None), "provider_http_5xx"),
        (HTTPError("https://synthetic.invalid", 403, "denied", {}, None), "provider_http_4xx"),
        (URLError("provider_transport_busy"), "provider_busy"),
        (URLError("provider_transport_unavailable"), "provider_executor_unavailable"),
        (ValueError("synthetic parse error"), "parse_error"),
    ],
)
def test_native_transport_failures_preserve_reusable_context(native, monkeypatch, failure, outcome):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(vin_lookup, "_vpic_request_json", fail)
    failed = native("decode_vin_vpic", identifier=VIN)
    assert failed["outcome"] == outcome
    assert failed["data"]["input_binding"] == binding(VIN)
    assert failed["data"]["source_failure"] == {"class": "unavailable", "reason": outcome}
    assert_reusable(native, failed)


@pytest.mark.parametrize(
    ("rows", "outcome"),
    [
        ([], "empty"),
        ([{"VIN": VIN, "ErrorCode": "0"}], "empty"),
        ([{"VIN": VIN, "Make": [], "ErrorCode": "0"}], "parse_error"),
    ],
)
def test_native_empty_and_parse_outcomes_allow_exact_echo(native, monkeypatch, rows, outcome):
    monkeypatch.setattr(vin_lookup, "_vpic_request_json", lambda *_args, **_kwargs: {"Results": rows})
    failed = native("decode_vin_vpic", identifier=VIN)
    assert failed["outcome"] == outcome
    if rows:
        assert failed["data"]["identifier_binding"] == {"status": "exact", "verified": True}
    assert_reusable(native, failed)


@pytest.mark.parametrize("tool", ["vininfo_decode", "corgi_decode"])
@pytest.mark.parametrize("reason", ["dependency_missing", "database_missing", "configuration_missing"])
def test_native_offline_runtime_failure_is_request_bound(native, monkeypatch, tool, reason):
    monkeypatch.setattr(offline, "_runtime", lambda *_args: (None, {}, reason))
    failed = native(tool, identifier=VIN)
    assert failed["outcome"] == reason
    assert failed["data"]["input_binding"] == binding(VIN)
    assert_reusable(native, failed)


@pytest.mark.parametrize("tool", ["vininfo_decode", "corgi_decode"])
@pytest.mark.parametrize("reason", ["parse_error", "decoder_timeout"])
def test_native_offline_worker_failure_is_request_bound(native, monkeypatch, tmp_path, tool, reason):
    node = tmp_path / "synthetic-node"
    node.write_text("synthetic executable; never run")
    (tmp_path / "vpic.lite.db").write_text("synthetic database; never read")
    manifest = {"node": {"executable": str(node), "sha256": offline._sha256(node)}}
    monkeypatch.setattr(offline, "_runtime", lambda *_args: (tmp_path, manifest, None))
    monkeypatch.setattr(offline, "_worker", lambda *_args: ({}, reason))
    failed = native(tool, identifier=VIN)
    assert failed["outcome"] == "parse_error" and reason in failed["warnings"]
    assert_reusable(native, failed)


@pytest.mark.parametrize(("wmi", "identifier"), [("WBA", VIN), ("W09000", "W09" + "0" * 14), ("AAA", "AAA" + "0" * 14)])
def test_native_wmi_empty_or_unsupported_uses_wmi_request_binding(native, monkeypatch, wmi, identifier):
    def fail(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(vin_lookup, "_vpic_request_json", fail)
    failed = native("decode_wmi_vpic", wmi=wmi)
    assert failed["data"]["input_binding"] == binding(wmi, "wmi")
    assert_reusable(native, failed, identifier=identifier)
    if wmi == "AAA":
        failed_local = native("decode_wmi_local", wmi=wmi)
        assert failed_local["outcome"] == "unsupported"
        assert_reusable(native, failed_local, identifier=identifier)


@pytest.mark.parametrize("location", ["data", "envelope", "evidence"])
@pytest.mark.parametrize("wmi", ["JHL", {}, [], False, "", 123])
def test_matching_vin_digest_never_overrides_foreign_or_malformed_wmi(native, monkeypatch, location, wmi):
    def fail(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(vin_lookup, "_vpic_request_json", fail)
    failed = native("decode_wmi_vpic", wmi="JHL")
    failed["data"]["input_binding"] = binding(VIN)
    failed["data"]["wmi"] = "WBA"
    target = failed["data"] if location == "data" else failed if location == "envelope" else failed["evidence"][0]
    target["wmi"] = wmi
    merged = native("reconcile_vehicle_identity", identifier=VIN, results=[failed], context=CONTEXT)
    assert merged["conflicts"] and not merged["data"]["source_failures"]
    assert ready_identity(merged, VIN)[0] is None


def test_matching_frame_digest_never_accepts_wmi_source(native, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(vin_lookup, "_vpic_request_json", fail)
    failed = native("decode_wmi_vpic", wmi="WBA")
    identifier = "ES1-1234567"
    failed["data"]["input_binding"] = binding(identifier, "frame_number")
    merged = native(
        "reconcile_vehicle_identity",
        identifier=identifier,
        identifier_type="frame_number",
        results=[failed],
        context={"make": "Honda", "model": "Civic"},
    )
    assert merged["conflicts"] and not merged["data"]["source_failures"]
    assert ready_identity(merged, identifier, identifier_type="frame_number")[0] is None


@pytest.mark.parametrize("foreign", [False, True])
def test_native_unverified_and_mismatched_facts_still_block(native, monkeypatch, foreign):
    row = {"Make": "Skoda", "ErrorCode": "0"}
    if foreign:
        row["VIN"] = "WBA" + "1" * 14
    monkeypatch.setattr(vin_lookup, "_vpic_request_json", lambda *_args, **_kwargs: {"Results": [row]})
    failed = native("decode_vin_vpic", identifier=VIN)
    merged = native("reconcile_vehicle_identity", identifier=VIN, results=[failed], context=CONTEXT)
    assert merged["conflicts"] and not merged["data"]["source_failures"]
    assert ready_identity(merged, VIN)[0] is None


@pytest.mark.parametrize("unsafe", ["unbound", "forged_class", "invalid_input", "evidence_mismatch", "exact_false"])
def test_source_failure_class_does_not_authenticate_or_hide_conflicts(native, monkeypatch, unsafe):
    monkeypatch.setattr(offline, "_runtime", lambda *_args: (None, {}, "dependency_missing"))
    failed = native("vininfo_decode", identifier=VIN)
    if unsafe == "unbound":
        failed["data"].pop("input_binding")
    elif unsafe == "forged_class":
        failed["outcome"] = "arbitrary_caller_reason"
        failed["data"]["source_failure"] = {"class": "unavailable", "reason": failed["outcome"]}
    elif unsafe == "invalid_input":
        failed["outcome"] = "invalid_input"
    elif unsafe == "exact_false":
        failed["data"]["identifier_binding"] = {"status": "exact", "verified": False}
    else:
        failed["evidence"] = [{"identifier_binding": {"status": "mismatch", "verified": False}}]
    merged = native("reconcile_vehicle_identity", identifier=VIN, results=[failed], context=CONTEXT)
    assert merged["conflicts"] and not merged["data"]["source_failures"]
    assert ready_identity(merged, VIN)[0] is None


@pytest.mark.parametrize(
    "outcome", ["provider_busy", "provider_executor_unavailable", "deadline_exceeded", "provider_error"]
)
def test_native_selected_decoder_failures_are_bound_after_validation(native, monkeypatch, outcome):
    class RejectingExecutor:
        async def run(self, operation):
            if outcome == "provider_error":
                return operation()
            raise ListingExecutionError(outcome, stage="admission")

    def fail(*_args, **_kwargs):
        raise OSError("synthetic decoder failure")

    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", RejectingExecutor())
    monkeypatch.setattr(identity, "_selected_decoders", lambda: {"vininfo_decode": fail})
    if outcome == "deadline_exceeded":
        token = read_execution_deadline.set(0)
    else:
        token = None
    try:
        result = native("decode_vehicle_batch", items=[VIN, "invalid"], decoder="vininfo_decode")
    finally:
        if token is not None:
            read_execution_deadline.reset(token)
    failed, invalid = [item["result"] for item in result["data"]["items"]]
    assert failed["outcome"] == outcome
    assert failed["data"]["input_binding"] == binding(VIN)
    assert "input_binding" not in invalid["data"]
    assert_reusable(native, failed)


@pytest.mark.parametrize("field", AGGREGATES)
def test_native_compare_respects_multiunit_fields_and_retains_unknowns(native, field):
    decoded = native(
        "decode_vehicle_identity",
        identifier=VIN,
        **CONTEXT,
        **{field: "Known-A"},
        live_vpic=False,
        live_wmi=False,
    )
    context = decoded["vehicle_profile"]
    wrong, matching, unknown = [
        {"id": label, **values}
        for label, values in (
            ("wrong", {field: "Other-B"}),
            ("matching", {field: "Known-A"}),
            ("unknown", {}),
        )
    ]
    for candidates in ([wrong, matching], [matching, wrong]):
        compared = native("compare_vehicle_modifications", context=context, candidates=candidates)
        assert compared["data"]["selected"] == matching
        assert compared["data"]["alternatives"] == [matching]
        assert any(conflict["field"] == field for conflict in compared["conflicts"])
    compared = native("compare_vehicle_modifications", context=context, candidates=[wrong, matching, unknown])
    assert compared["data"]["selected"] is None
    assert compared["data"]["alternatives"] == [matching, unknown]
    compared = native("compare_vehicle_modifications", context=CONTEXT, candidates=[wrong, matching])
    assert compared["data"]["selected"] is None and field in compared["missing_fields"]


@pytest.mark.parametrize("with_context", [False, True])
def test_native_standalone_group_wmi_does_not_dispute_marque_or_manufacturer(native, monkeypatch, with_context):
    identifier = "XW8AC2NH" + "0" * 9
    monkeypatch.setattr(
        vin_lookup,
        "_vpic_request_json",
        lambda *_args, **_kwargs: {
            "Results": [
                {
                    "VIN": identifier,
                    "Make": "Skoda",
                    "Model": "Rapid",
                    "Manufacturer": "SKODA AUTO A.S.",
                    "ErrorCode": "0",
                }
            ]
        },
    )
    if with_context:
        strong = native(
            "decode_vehicle_identity",
            identifier=identifier,
            make="Skoda",
            model="Rapid",
            live_vpic=False,
            live_wmi=False,
        )
    else:
        strong = native("decode_vin_vpic", identifier=identifier)
    weak = native("decode_wmi_local", wmi="XW8")
    assert weak["data"]["hints"]["make"] == "Volkswagen Group"
    assert "make" not in weak["data"]["vehicle_profile"]
    assert "manufacturer" not in weak["data"]["vehicle_profile"]
    before = deepcopy([strong, weak])
    for rows in itertools.permutations([strong, weak]):
        merged = native("reconcile_vehicle_identity", identifier=identifier, results=list(rows))
        assert merged["conflicts"] == []
        assert merged["data"]["vehicle_profile"]["make"] == "Skoda"
        if not with_context:
            assert merged["data"]["vehicle_profile"]["manufacturer"] == "SKODA AUTO A.S."
        accepted, errors = ready_identity(merged, identifier)
        assert accepted is not None and errors == []
    caller = {"input_binding": binding(identifier), "vehicle_profile": {"make": "Volkswagen Group"}}
    merged = native("reconcile_vehicle_identity", identifier=identifier, results=[strong, caller])
    assert merged["conflicts"] and ready_identity(merged, identifier)[0] is None
    assert caller == {"input_binding": binding(identifier), "vehicle_profile": {"make": "Volkswagen Group"}}
    assert [strong, weak] == before
