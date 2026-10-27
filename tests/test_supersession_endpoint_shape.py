"""A structured source number cannot prove one supersession endpoint."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_parts as parts
from autostop_manager.automotive_mcp import register_automotive_tools


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("supersession evidence tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(params=["direct", "native-object", "native-json"])
def compare(request):
    native = None
    if request.param != "direct":
        server = FastMCP("synthetic-supersession-endpoint-shape")
        register_automotive_tools(server)
        native = server._tool_manager.get_tool("compare_part_relations")

    def call(rows):
        before = deepcopy(rows)
        if native is None:
            response = parts.compare_part_relations(rows)
        else:
            supplied = json.dumps(rows) if request.param == "native-json" else rows
            content, response = asyncio.run(native.run({"relations": supplied}, convert_result=True))
            assert json.loads(content[0].text) == response
        assert rows == before
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        for actual, original in zip(response["data"]["relations"], before, strict=True):
            assert actual["fitment_confirmed"] is False
            assert actual["evidence"] == original["evidence"]
            assert actual["qualifiers"] == original["qualifiers"]
            for endpoint in ("from", "to"):
                assert actual[endpoint]["number"] == original[endpoint]["number"]
                assert actual[endpoint]["brand"] == original[endpoint]["brand"]
        return response

    return call


def _edge(left="DEMO123", right="OTHER456", direction="from_to"):
    row = {
        "type": "supersession",
        "direction": direction,
        "from": {"number": left, "brand": "DEMO"},
        "to": {"number": right, "brand": "DEMO"},
        "qualifiers": [{"engine": "SYNTHETIC", "date_from": "2020-01"}],
    }
    row["evidence"] = [
        {
            "document_kind": "manufacturer_supersession",
            "provider": "SYNTHETIC manufacturer fixture",
            "primary_lineage": "synthetic.publication.v1",
            "method": "document_read",
            "locator": "https://example.org/synthetic-supersession",
            "fetched_at": "2026-01-01T00:00:00Z",
            "direction": direction,
            "from": deepcopy(row["from"]),
            "to": deepcopy(row["to"]),
        }
    ]
    return row


@pytest.mark.parametrize("direction", ["from_to", "to_from"])
@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize(
    "source_number,requested",
    [
        pytest.param(["DEMO123"], "DEMO123", id="singleton-array"),
        pytest.param(["DEMO", "123"], "DEMO123", id="array-fragments"),
        pytest.param({"DEMO": "123"}, "DEMO123", id="object"),
        pytest.param(True, "TRUE", id="boolean-true"),
        pytest.param(False, "FALSE", id="boolean-false"),
    ],
)
def test_collection_or_boolean_source_number_cannot_verify_endpoint(
    compare, direction, endpoint, source_number, requested
):
    row = _edge(direction=direction)
    row[endpoint]["number"] = requested
    row["evidence"][0][endpoint]["number"] = source_number

    response = compare([row])

    assert response["outcome"] == "partial"
    assert response["data"]["relations"][0]["type"] == "unverified_supersession"
    assert response["missing_fields"] == ["relations[0].supersession_direction_and_primary_evidence"]
    assert response["conflicts"] == []


@pytest.mark.parametrize("direction", ["from_to", "to_from"])
@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize(
    "source_number,requested",
    [("de-mo 123", "DEMO123"), (1234, "1234"), (12.5, "125")],
    ids=["formatted-string", "integer-scalar", "float-scalar"],
)
def test_matching_scalar_source_number_keeps_existing_policy(compare, direction, endpoint, source_number, requested):
    row = _edge(direction=direction)
    row[endpoint]["number"] = requested
    row["evidence"][0][endpoint]["number"] = source_number

    response = compare([row])

    assert response["outcome"] == "success"
    assert response["data"]["relations"][0]["type"] == "supersession"
    assert response["missing_fields"] == response["conflicts"] == []


@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize("valid_first", [True, False])
def test_one_valid_source_still_proves_edge_among_malformed_sources(compare, endpoint, valid_first):
    row = _edge()
    valid = row["evidence"][0]
    malformed = deepcopy(valid)
    malformed[endpoint]["number"] = ["DEMO", "123"] if endpoint == "from" else ["OTHER", "456"]
    row["evidence"] = [valid, malformed] if valid_first else [malformed, valid]

    response = compare([row])

    assert response["outcome"] == "success"
    assert response["data"]["relations"][0]["type"] == "supersession"
    assert response["missing_fields"] == response["conflicts"] == []


@pytest.mark.parametrize("direction", ["from_to", "to_from"])
def test_malformed_source_cannot_create_a_conflicting_replacement_branch(compare, direction):
    if direction == "from_to":
        verified = _edge("DEMO123", "FIRST456", direction)
        malformed = _edge("DEMO123", "SECOND789", direction)
        source_endpoint = "from"
    else:
        verified = _edge("FIRST456", "DEMO123", direction)
        malformed = _edge("SECOND789", "DEMO123", direction)
        source_endpoint = "to"
    malformed["evidence"][0][source_endpoint]["number"] = ["DEMO", "123"]

    response = compare([verified, malformed])

    assert response["outcome"] == "partial"
    assert [row["type"] for row in response["data"]["relations"]] == ["supersession", "unverified_supersession"]
    assert response["missing_fields"] == ["relations[1].supersession_direction_and_primary_evidence"]
    assert response["conflicts"] == []


@pytest.mark.parametrize("kind", ["cross", "analog", "oe_reference"])
def test_other_relation_kinds_keep_supplied_evidence_without_supersession_confirmation(compare, kind):
    row = _edge()
    row["type"] = kind
    row["evidence"][0]["from"]["number"] = ["DEMO", "123"]

    response = compare([row])

    assert response["outcome"] == "success"
    assert response["data"]["relations"][0]["type"] == kind
    assert response["missing_fields"] == response["conflicts"] == []
