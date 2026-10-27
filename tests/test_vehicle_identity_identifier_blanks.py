"""Blank identifier aliases retain the next populated identifier or nested fact."""

import asyncio
from copy import deepcopy
import json
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_telemetry, mcp_tools, vehicle_identity as identity
from autostop_manager.automotive_contracts import binding
from autostop_manager.vehicle_identity_async import decode_vehicle_identities_async


VIN = "ZZZ" + "0" * 14
OTHER_VIN = "YYY" + "1" * 14
FRAME = "ZZZ99-000001"
ALIASES = ("identifier", "vin", "frame", "chassis_number", "body_number")
FACTS = {"make": "DEMO", "model": "Synthetic"}


@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("identifier normalization must not acquire provider, network, process or DB data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(identity, "catalog_provider_status", lambda: {"providers": []})
    monkeypatch.setattr(identity, "build_lookup_plan", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(identity, "load_source_registry", lambda: {"version": 0})


@pytest.fixture(scope="module")
def native_tool():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "StoreIntegration",
            "StoreApiClient",
            "StoreOwnerApiClient",
            "StoreQuoteConductor",
            "StoreQuoteOwnerApi",
        ):
            patch.setattr(mcp_tools, name, Inert)
        for name in (
            "get_store_api_url",
            "get_store_read_token",
            "get_store_manage_token",
            "get_store_quote_token",
            "get_store_owner_token",
        ):
            patch.setattr(mcp_tools, name, lambda: "")
        patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
        server = FastMCP("isolated-identity-blank-identifiers")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"decode_vehicle_identities"}
        )
        tool = server._tool_manager.get_tool("decode_vehicle_identities")
        assert tool.is_async
        yield tool


@pytest.fixture(params=["sync", "async", "native", "native-json"])
def decode(request, native_tool):
    def call(items):
        snapshot = deepcopy(items)
        if request.param == "sync":
            result = identity.decode_vehicle_identities(items, live_vpic=False)
        elif request.param == "async":
            result = asyncio.run(decode_vehicle_identities_async(items, live_vpic=False))
        else:
            arguments = {"items": json.dumps(items) if request.param == "native-json" else items, "live_vpic": False}
            content, result = asyncio.run(native_tool.run(arguments, convert_result=True))
            assert json.loads(content[0].text) == result
        assert items == snapshot
        assert result["count"] == len(items)
        assert [row["item_index"] for row in result["results"]] == list(range(len(items)))
        return result["results"]

    return call


@pytest.mark.parametrize("outer", ALIASES)
@pytest.mark.parametrize("empty", [None, "", " \t", "\u00a0\u2003"])
def test_blank_outer_alias_uses_populated_nested_vin(decode, outer, empty):
    row = decode([{outer: empty, "crm_context": {"vin": VIN, **FACTS}}])[0]

    assert row["ok"] is True
    assert row["identifier"]["kind"] == "vin"
    assert row["input_binding"] == binding(VIN)
    assert row["vehicle_profile"]["make"] == FACTS["make"]
    assert row["vehicle_profile"]["model"] == FACTS["model"]


@pytest.mark.parametrize("carrier", ["crm_context", "vehicle_profile", "vehicle_profile_compact"])
def test_blank_identifier_retains_nested_frame_and_explicit_type(decode, carrier):
    facts = {"frame": FRAME, **FACTS}
    context = (
        facts if carrier == "crm_context" else {carrier: json.dumps(facts) if carrier.endswith("compact") else facts}
    )
    item = {"identifier": "\t\u00a0", "identifier_type": "frame_number", "crm_context": context}

    row = decode([item])[0]

    assert row["ok"] is True
    assert row["identifier"]["kind"] == "frame_number"
    assert row["input_binding"] == binding(FRAME, "frame_number")


def test_blank_identifier_uses_next_populated_flat_alias(decode):
    row = decode([{"identifier": " \t", "vin": VIN, **FACTS}])[0]

    assert row["ok"] is True
    assert row["input_binding"] == binding(VIN)


@pytest.mark.parametrize("selected", ALIASES[:-1])
def test_nonempty_alias_keeps_its_existing_priority(decode, selected):
    position = ALIASES.index(selected)
    item = dict.fromkeys(ALIASES[position + 1 :], OTHER_VIN)
    item[selected] = VIN
    item.update(FACTS)

    row = decode([item])[0]

    assert row["ok"] is True
    assert row["input_binding"] == binding(VIN)


@pytest.mark.parametrize("bad", [False, 0, [], {}])
def test_malformed_selected_identifier_is_an_error_without_fallback_or_losing_other_rows(decode, bad):
    rows = decode(
        [
            {"identifier": VIN, **FACTS},
            {"identifier": bad, "vin": VIN, **FACTS},
            {"identifier": OTHER_VIN, **FACTS},
        ]
    )

    assert rows[0]["ok"] is True and rows[0]["input_binding"] == binding(VIN)
    assert rows[2]["ok"] is True and rows[2]["input_binding"] == binding(OTHER_VIN)
    assert rows[1]["ok"] is False
    assert rows[1]["input_binding"] is None
    assert rows[1]["vehicle_profile"] == {}
    assert rows[1]["errors"] == [{"code": "expected_string", "field": "identifier", "stage": "input_validation"}]


def test_all_blank_aliases_preserve_context_only_unknown_identifier(decode):
    row = decode([{**dict.fromkeys(ALIASES, " \t"), "crm_context": FACTS}])[0]

    assert row["ok"] is True
    assert row["identifier"]["kind"] == "unknown"
    assert row["input_binding"] == binding("")
