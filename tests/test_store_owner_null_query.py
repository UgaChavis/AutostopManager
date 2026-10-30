"""Nullable query values keep the same meaning in plans and synthetic HTTP requests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import socket
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_store_executor, mcp_telemetry, mcp_tools, store_owner_api as owner
from autostop_manager.listing_executor import BoundedListingExecutor


def capabilities():
    # FastAPI's current /parts query contract uses optional anyOf[type, null].
    parameters = [
        {"name": name, "in": "query", "required": False, "schema": schema}
        for name, schema in {
            "q": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "priceFrom": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "isActive": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "page": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}},
        }.items()
    ]
    response = {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}
    paths = {
        f"/api/v1/synthetic-{index}": {"get": {"operationId": f"synthetic_{index}", "responses": response}}
        for index in range(99)
    }
    paths["/api/v1/parts"] = {
        "get": {"operationId": "list_parts", "parameters": parameters, "responses": response},
        "post": {
            "operationId": "create_part",
            "parameters": parameters,
            "responses": response,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
        },
    }
    return owner._parse_capabilities({"openapi": "3.1.0", "paths": paths})


class Response(BytesIO):
    status = 200

    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = {"Content-Type": "application/json", **(headers or {})}


@pytest.fixture(scope="module")
def harness():
    class Inert:
        def __init__(self, *_args, **_kwargs):
            pass

    # Windows creates an internal socket pair when constructing its event loop.
    loop = asyncio.new_event_loop()
    client = owner.StoreOwnerApiClient(
        agent_api_url="http://127.0.0.1:8010/internal/agent/v1", owner_token="synthetic-token"
    )
    pools = [BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=3) for _ in range(2)]
    try:
        with pytest.MonkeyPatch.context() as patch:
            for name in ("StoreIntegration", "StoreApiClient", "StoreQuoteConductor", "StoreQuoteOwnerApi"):
                patch.setattr(mcp_tools, name, Inert)
            patch.setattr(mcp_tools, "StoreOwnerApiClient", lambda **_kwargs: client)
            for name in (
                "get_store_api_url",
                "get_store_read_token",
                "get_store_manage_token",
                "get_store_quote_token",
                "get_store_owner_token",
            ):
                patch.setattr(mcp_tools, name, lambda: "")
            patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
            patch.setattr(mcp_store_executor, "_READ_EXECUTOR", pools[0])
            patch.setattr(mcp_store_executor, "_STATEFUL_EXECUTOR", pools[1])
            server = FastMCP("synthetic-owner-null-query")
            mcp_tools.register_manager_tools(
                server, store=Inert(), store_client=Inert(), include_tools={"store_owner_api"}
            )
            yield client, server._tool_manager.get_tool("store_owner_api"), loop
    finally:
        for pool in pools:
            pool.close()
        loop.close()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, harness):
    def denied(*_args, **_kwargs):
        raise AssertionError("synthetic owner query test must not use network or runtime data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    client, _tool, _loop = harness
    client._capabilities = capabilities()
    client._cached_at = owner.time.monotonic()
    monkeypatch.setattr(client, "_read_openapi", denied)
    monkeypatch.setattr(owner, "_open_response", denied)


@pytest.fixture(params=["direct", "native"])
def call(request, harness):
    client, tool, loop = harness

    def invoke(**arguments):
        if request.param == "direct":
            return client.invoke(**arguments)
        content, result = loop.run_until_complete(tool.run(arguments, convert_result=True))
        assert json.loads(content[0].text) == result
        return result

    return invoke


@pytest.fixture
def sent(monkeypatch):
    requests = []

    def opened(request, **_kwargs):
        requests.append(request)
        return Response({"items": []})

    monkeypatch.setattr(owner, "_open_response", opened)
    return requests


@pytest.mark.parametrize(
    "query", [{"q": None}, {"priceFrom": None}, {"isActive": None}, {"q": None, "priceFrom": None}]
)
def test_optional_nullable_query_matches_absent_in_wire_and_plan(call, harness, sent, query):
    client, _tool, _loop = harness
    absent = client.prepare_invocation(operation_id="list_parts")
    prepared = client.prepare_invocation(operation_id="list_parts", query=query)
    assert absent["ok"] and prepared["ok"]
    for field in ("query_sha256", "query_fields", "plan_hash"):
        assert prepared["summary"][field] == absent["summary"][field]
    result = call(operation_id="list_parts", mode="read", query=query)
    assert result["ok"] is True
    assert len(sent) == 1 and urlsplit(sent[0].full_url).query == ""


@pytest.mark.parametrize("mode", ["read", "dry_run", "apply", "revision"])
def test_required_nullable_query_blocks_before_any_dispatch(call, harness, mode):
    client, _tool, _loop = harness
    operation_id = "list_parts" if mode == "read" else "create_part"
    client._capabilities[operation_id] = replace(client._capabilities[operation_id], required_query_parameters=("q",))
    prepared = client.prepare_invocation(
        operation_id=operation_id, query={"q": None}, body={} if mode != "read" else None
    )
    assert prepared["ok"] is False
    assert prepared["error"]["code"] == "store_owner_query_null_unrepresentable"
    result = call(operation_id=operation_id, mode=mode, query={"q": None}, body={} if mode != "read" else None)
    assert result["ok"] is False and result["error"]["code"] == "store_owner_query_null_unrepresentable"
    assert not result["meta"].get("request_dispatched")


@pytest.mark.parametrize(
    "query,wire",
    [
        ({"q": "None"}, "q=None"),
        ({"q": ""}, "q="),
        ({"isActive": False}, "isActive=False"),
        ({"priceFrom": 0}, "priceFrom=0"),
        ({"tags": [None, False, 0, "None", ""]}, "tags=None&tags=False&tags=0&tags=None&tags="),
        ({"tags": []}, ""),
        ({"q": None, "isActive": False, "priceFrom": 0}, "isActive=False&priceFrom=0"),
    ],
)
def test_non_null_values_and_array_elements_keep_existing_wire(call, sent, query, wire):
    assert call(operation_id="list_parts", mode="read", query=query)["ok"] is True
    assert len(sent) == 1 and urlsplit(sent[0].full_url).query == wire


@pytest.mark.parametrize(
    "query,code",
    [
        ({"page": None}, "store_owner_query_schema_invalid"),
        ({"unknown": None}, "store_owner_query_parameters_mismatch"),
    ],
)
def test_null_does_not_bypass_existing_schema_or_name_validation(call, query, code):
    result = call(operation_id="list_parts", mode="read", query=query)
    assert result["ok"] is False and result["error"]["code"] == code


def test_null_and_literal_none_have_distinct_plans_but_null_and_absent_match(harness, sent):
    client, _tool, _loop = harness
    null = client.prepare_invocation(operation_id="list_parts", query={"q": None})["summary"]
    literal = client.prepare_invocation(operation_id="list_parts", query={"q": "None"})["summary"]
    assert null["plan_hash"] != literal["plan_hash"]
    assert null["query_sha256"] != literal["query_sha256"]
    assert client.invoke(operation_id="list_parts", mode="read", query={}, expected_plan_hash=null["plan_hash"])["ok"]
    blocked = client.invoke(
        operation_id="list_parts", mode="read", query={"q": "None"}, expected_plan_hash=null["plan_hash"]
    )
    assert blocked["error"]["code"] == "store_owner_plan_changed" and len(sent) == 1


def test_native_prepare_uses_the_same_plan_for_optional_null_and_absent(harness, monkeypatch):
    client, tool, loop = harness
    plans = []
    original = client.prepare_invocation

    def record_plan(**arguments):
        prepared = original(**arguments)
        plans.append(prepared["summary"]["plan_hash"])
        return prepared

    monkeypatch.setattr(client, "prepare_invocation", record_plan)
    for query in ({}, {"q": None}, {"q": "None"}):
        content, result = loop.run_until_complete(
            tool.run({"operation_id": "list_parts", "mode": "prepare", "query": query}, convert_result=True)
        )
        assert json.loads(content[0].text) == result
        assert result["ok"] is True and result["status"] == "validated"
        assert result["meta"]["request_dispatched"] is False
    assert plans[0] == plans[1] != plans[2]


def test_nullable_query_dry_run_apply_and_retry_keep_exact_request_bytes(harness, monkeypatch):
    client, _tool, _loop = harness
    proof = "b" * 64
    requests = []

    def opened(request, **_kwargs):
        requests.append(request)
        if len(requests) == 1:
            return Response(
                {
                    "ok": True,
                    "mode": "dry_run",
                    "dryRunProof": proof,
                    "receiptId": "synthetic-receipt",
                    "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "currentRevision": None,
                    "revisionKind": "revision_exempt",
                    "contractVersion": owner.OWNER_PREFLIGHT_CONTRACT_VERSION,
                },
                {owner.DRY_RUN_PROOF_HEADER: proof},
            )
        return Response({"id": "synthetic-part"})

    monkeypatch.setattr(owner, "_open_response", opened)
    arguments = {
        "operation_id": "create_part",
        "body": {"name": "synthetic-part", "comment": None},
        "owner_intent": "Synthetic test only",
        "correlation_id": "synthetic-correlation",
    }
    planned = client.invoke(**arguments, mode="dry_run", query={"q": None}, idempotency_key="synthetic-dry-run")
    assert planned["ok"] is True
    for query in ({}, {"q": None}):
        result = client.invoke(
            **arguments,
            mode="apply",
            query=query,
            idempotency_key="synthetic-apply",
            dry_run_proof=proof,
            expected_plan_hash=planned["summary"]["plan_hash"],
        )
        assert result["ok"] is True
    assert [request.get_method() for request in requests] == ["POST"] * 3
    assert len({request.full_url for request in requests}) == 1
    assert urlsplit(requests[0].full_url).query == ""
    assert requests[0].data == requests[1].data == requests[2].data
    assert json.loads(requests[0].data)["comment"] is None
    assert requests[1].header_items() == requests[2].header_items()
