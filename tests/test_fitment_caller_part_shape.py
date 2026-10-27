"""Caller part-number containers cannot establish a single part's fitment."""

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager.automotive_mcp import register_automotive_tools
from autostop_manager.automotive_parts import assess_part_fitment


REF = {"provider": "partsapi_ru", "namespace": "tecdoc", "entity_kind": "modification", "id": "42", "carType": "PC"}
# An artificial binding digest; no vehicle identifier is used or decoded.
BINDING = {"version": 1, "identifier_kind": "market_code", "identifier_sha256": "a" * 64}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("ready fitment evidence must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    server = FastMCP("isolated-caller-part-fitment")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("assess_part_fitment")


@pytest.fixture
def call(native_tool):
    def invoke(arguments, *, mode="direct"):
        original = deepcopy(arguments)
        if mode == "direct":
            response = assess_part_fitment(**arguments)
        else:
            wire = {
                key: json.dumps(value) if mode == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in arguments.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert arguments == original
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        assert response["data"]["part"] == original["part"]
        for supplied, returned in zip(original["evidence"], response["evidence"], strict=True):
            assert all(returned[key] == value for key, value in supplied.items())
        return response

    return invoke


def _arguments(part, source_number="DEMO123", scope="modification"):
    return {
        "vehicle": {
            "vehicle_profile": {"make": "DEMO", "engine": "DEMO-E1"},
            "catalog_ref": deepcopy(REF),
            "input_binding": deepcopy(BINDING),
        },
        "part": {"brand": "DEMO", "request_tag": "synthetic", **part},
        "criteria": {"engine": "DEMO-E1"},
        "evidence": [
            {
                "provider": "manufacturer",
                "primary_lineage": "synthetic.official.document",
                "method": "supplied_document",
                "locator": "https://example.org/synthetic-fitment",
                "fetched_at": "2026-01-01T00:00:00Z",
                "document_kind": "official_epc",
                "fitment_assertion": True,
                "scope": scope,
                "part_number": source_number,
                "brand": "DEMO",
                "catalog_ref": deepcopy(REF),
                "identifier_binding": deepcopy(BINDING),
            }
        ],
        "scope": scope,
    }


@pytest.mark.parametrize(
    "alias,number,source_number",
    [
        ("number", ["DEMO", "123"], "DEMO123"),
        ("number", ["DEMO123"], "DEMO123"),
        ("number", {"DEMO": "123"}, "DEMO123"),
        ("number", True, "TRUE"),
        ("part_number", ["DEMO", "123"], "DEMO123"),
        ("normalized_number", {"DEMO": "123"}, "DEMO123"),
        ("raw_number", True, "TRUE"),
    ],
)
def test_selected_container_or_boolean_never_binds_one_part(call, alias, number, source_number):
    response = call(_arguments({alias: number}, source_number))

    assert response["outcome"] == "partial"
    assert response["data"]["state"] == "unknown"
    assert "primary_bound_fitment_evidence" in response["missing_fields"]
    assert response["conflicts"] == []


@pytest.mark.parametrize("scope", ["family", "exact_identifier"])
def test_caller_array_also_cannot_prove_family_or_exact_fitment(call, scope):
    response = call(_arguments({"number": ["DEMO", "123"]}, scope=scope))

    assert response["data"]["state"] == "unknown"
    assert "primary_bound_fitment_evidence" in response["missing_fields"]


@pytest.mark.parametrize(
    "alias,number,source_number",
    [
        ("number", "de-mo 123", "DEMO123"),
        ("number", 1234, "1234"),
        ("part_number", 1234, 1234),
        ("normalized_number", 12.5, "125"),
        ("raw_number", "DEMO-123", "DEMO123"),
    ],
)
def test_formatted_strings_and_numeric_scalars_keep_existing_binding(call, alias, number, source_number):
    response = call(_arguments({alias: number}, source_number))

    assert response["outcome"] == "success" and response["data"]["state"] == "supported"
    assert response["missing_fields"] == response["conflicts"] == []


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_single_matching_part_keeps_each_scope_available(call, scope):
    response = call(_arguments({"number": "DEMO-123"}, scope=scope))

    assert response["data"]["state"] == "supported" and response["data"]["scope"] == scope
    assert response["missing_fields"] == response["conflicts"] == []


@pytest.mark.parametrize(
    "part,source_number,state",
    [
        ({"number": None, "part_number": "DEMO-123"}, "DEMO123", "supported"),
        ({"number": 0, "part_number": "DEMO-123"}, "DEMO123", "supported"),
        ({"number": False, "part_number": "DEMO-123"}, "DEMO123", "supported"),
        ({"number": [], "part_number": "DEMO-123"}, "DEMO123", "supported"),
        ({"part_number": {}, "normalized_number": "DEMO123"}, "DEMO123", "supported"),
        (
            {"number": "", "part_number": None, "normalized_number": "DEMO-123", "raw_number": "OTHER"},
            "DEMO123",
            "supported",
        ),
        ({"number": "OTHER", "part_number": "DEMO123"}, "DEMO123", "unknown"),
        ({"number": ["DEMO", "123"], "part_number": "DEMO123"}, "DEMO123", "unknown"),
        ({"number": False}, "FALSE", "unknown"),
        ({"number": 0}, "0", "unknown"),
    ],
)
def test_existing_first_truthy_alias_selection_is_preserved(call, part, source_number, state):
    response = call(_arguments(part, source_number))

    assert response["data"]["state"] == state
    assert response["outcome"] == ("success" if state == "supported" else "partial")


@pytest.mark.parametrize("case", ["foreign_binding", "unknown_engine", "conflicting_engine", "unparsed_restriction"])
def test_a_single_part_still_requires_bound_and_satisfied_conditions(call, case):
    arguments = _arguments({"number": "DEMO123"})
    expected = "unknown"
    if case == "foreign_binding":
        arguments["evidence"][0]["catalog_ref"]["id"] = "43"
    elif case == "unknown_engine":
        arguments["vehicle"]["vehicle_profile"].pop("engine")
    elif case == "conflicting_engine":
        arguments["evidence"][0]["conditions"] = {"engine": "DEMO-E2"}
        expected = "rejected"
    else:
        arguments["evidence"][0]["unparsed_restrictions"] = ["synthetic unresolved condition"]

    response = call(arguments)

    assert response["data"]["state"] == expected
    assert response["conflicts"] if expected == "rejected" else response["missing_fields"]


@pytest.mark.parametrize("mode", ["native-object", "native-json"])
@pytest.mark.parametrize(
    "part,source_number,state",
    [
        ({"number": ["DEMO", "123"]}, "DEMO123", "unknown"),
        ({"number": True}, "TRUE", "unknown"),
        ({"raw_number": 1234}, 1234, "supported"),
        ({"number": [], "part_number": "DEMO123"}, "DEMO123", "supported"),
        ({"number": "OTHER", "part_number": "DEMO123"}, "DEMO123", "unknown"),
    ],
)
def test_native_wire_shapes_preserve_binding_and_alias_policy(call, mode, part, source_number, state):
    response = call(_arguments(part, source_number), mode=mode)

    assert response["data"]["state"] == state
    assert response["outcome"] == ("success" if state == "supported" else "partial")
