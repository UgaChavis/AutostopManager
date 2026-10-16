"""029: session evidence and fingerprints compare the same JSON observations.

Twelve distinct cases plus one AppServer consumer repeat (13 pytest runs).
Real tool registration, schema generation, fingerprints and comparison execute.
Only unrelated Store construction/configuration and AppServer request are fake;
no server, tool backend, network or process is started.
Array order and serialized numeric identity follow the existing fingerprint
contract, not semantic equivalence of all possible JSON Schema documents.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_tools
from autostop_manager.mcp_contract import (
    manager_session_schema_evidence,
    mcp_schema_fingerprint,
    validate_manager_mcp_surface,
)
from autostop_manager.storage import StoreState
from autostop_manager.telegram_wake import AppServer, WakeConfig


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _roundtrip(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


@pytest.fixture
def schemas(tmp_path, monkeypatch):
    class InertAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    def forbidden(*_args, **_kwargs):
        raise AssertionError("schema proof attempted an external boundary")

    # Registration is real; constructors and configuration are not schema input.
    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, InertAdapter)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    server = FastMCP("synthetic-schema-review")
    database = tmp_path / "unused-memory.sqlite3"
    mcp_tools.register_manager_tools(server, store=StoreState(database))
    surface = {name: _roundtrip(tool.parameters) for name, tool in server._tool_manager._tools.items()}
    assert validate_manager_mcp_surface(surface)["ok"] is True
    yield surface
    assert not database.exists()


def _evidence(expected, app, *, connected=True, tools_error=False, declarations):
    before = _json((expected, app, declarations))
    result = manager_session_schema_evidence(
        expected, app, connected=connected, tools_error=tools_error, declarations=declarations
    )
    assert _json((expected, app, declarations)) == before
    assert result["reload_guarantees_declarations"] is False
    return result


def _changed_default(schemas, name, parameter, original, replacement):
    changed = deepcopy(schemas)
    field = changed[name]["properties"][parameter]
    assert type(field["default"]) is type(original)
    assert field["default"] == original
    field["default"] = replacement
    return _roundtrip(changed)


def _assert_drift(schemas, changed, name, *, observation="app"):
    before = _json((schemas, changed))
    assert mcp_schema_fingerprint(changed) != mcp_schema_fingerprint(schemas)
    manifest = validate_manager_mcp_surface(changed)
    assert manifest["ok"] is False and manifest["diagnostic"] == "schema_mismatch"
    assert manifest["missing_registered_tools"] == manifest["unexpected_registered_tools"] == []
    result = _evidence(
        schemas,
        changed if observation == "app" else schemas,
        declarations=changed if observation == "declarations" else schemas,
    )
    assert _json((schemas, changed)) == before
    assert result["ok"] is False
    assert result["expected_tool_count"] == result["app_tool_count"] == len(schemas)
    if observation == "app":
        assert result["diagnostic"] == "app_schema_stale_or_unavailable"
        assert result["app_schema_matches"] is False
        assert result["app_schema_drift_tools"] == [name]
        assert result["declarations_match"] is True
        assert result["declarations_drift_tools"] == []
    else:
        assert result["diagnostic"] == "model_declarations_stale"
        assert result["app_schema_matches"] is True
        assert result["app_schema_drift_tools"] == []
        assert result["declarations_match"] is False
        assert result["declarations_drift_tools"] == [name]


@pytest.mark.parametrize("observation", ("app", "declarations"))
@pytest.mark.parametrize(
    ("name", "parameter", "original", "replacement"),
    [
        ("manager_automations", "include_archived", False, 0),
        ("lookup_original_parts", "live_vpic", True, 1),
    ],
)
def test_real_boolean_default_drift_is_not_schema_evidence(
    schemas, observation, name, parameter, original, replacement
):
    assert schemas[name]["properties"][parameter]["type"] == "boolean"
    changed = _changed_default(schemas, name, parameter, original, replacement)
    _assert_drift(schemas, changed, name, observation=observation)


@pytest.mark.parametrize("observed", (0.0, -0.0), ids=("integer-to-float", "negative-zero"))
def test_serialized_number_identity_matches_fingerprint(schemas, observed):
    changed = _changed_default(schemas, "j1_research_results", "cursor", 0, observed)
    _assert_drift(schemas, changed, "j1_research_results")


def _reverse_object_order(value):
    if isinstance(value, dict):
        return {key: _reverse_object_order(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_object_order(item) for item in value]
    return value


def test_object_key_order_preserves_verification_without_mutating_observations(schemas):
    observed = _reverse_object_order(_roundtrip(schemas))
    before = _json((schemas, observed))
    assert list(observed) == list(reversed(schemas))
    assert mcp_schema_fingerprint(observed) == mcp_schema_fingerprint(schemas)
    assert validate_manager_mcp_surface(observed)["ok"] is True
    result = _evidence(schemas, observed, declarations=deepcopy(observed))
    assert result["ok"] is True and result["diagnostic"] == "schema_verified"
    assert result["app_schema_drift_tools"] == result["declarations_drift_tools"] == []
    assert _json((schemas, observed)) == before


def test_array_order_is_preserved_by_schema_observation_comparison(schemas):
    changed = deepcopy(schemas)
    values = changed["manager_automations"]["properties"]["operation"]["enum"]
    assert values == ["status", "templates", "readiness"]
    values.reverse()
    _assert_drift(schemas, _roundtrip(changed), "manager_automations")


@pytest.mark.parametrize("change", ("number", "missing"))
def test_null_default_is_distinct_from_number_and_absent_key(schemas, change):
    changed = deepcopy(schemas)
    field = changed["j1_research_document"]["properties"]["page"]
    assert field["default"] is None
    if change == "number":
        field["default"] = 0
    else:
        del field["default"]
    _assert_drift(schemas, _roundtrip(changed), "j1_research_document")


def test_missing_tools_are_reported_in_both_observations(schemas):
    observed = deepcopy(schemas)
    del observed["manager_automations"]
    result = _evidence(schemas, observed, declarations=deepcopy(observed))
    assert result["ok"] is False
    assert result["diagnostic"] == "app_schema_stale_or_unavailable"
    assert result["expected_tool_count"] == len(schemas)
    assert result["app_tool_count"] == len(schemas) - 1
    assert result["app_schema_matches"] is False and result["declarations_match"] is False
    assert result["app_schema_drift_tools"] == result["declarations_drift_tools"] == ["manager_automations"]


def test_matching_schemas_do_not_attest_a_disconnected_app(schemas):
    result = _evidence(schemas, deepcopy(schemas), connected=False, declarations=deepcopy(schemas))
    assert result["ok"] is False and result["diagnostic"] == "app_schema_stale_or_unavailable"
    assert result["app_schema_matches"] is False and result["declarations_match"] is True
    assert result["app_schema_drift_tools"] == result["declarations_drift_tools"] == []


def test_app_status_preserves_real_boolean_drift_without_private_output(schemas, monkeypatch):
    changed = _changed_default(schemas, "manager_automations", "include_archived", False, 0)
    app = AppServer(WakeConfig("private-synthetic-thread"))
    page = {
        "data": [
            {
                "name": "autostopmanager",
                "runtimeStatus": "connected",
                "toolsError": None,
                "httpOrigin": "PRIVATE_ORIGIN",
                "tools": {name: {"name": name, "inputSchema": schema} for name, schema in changed.items()},
            }
        ],
        "nextCursor": None,
    }
    before = _json((schemas, changed, page))
    requests = []

    async def request(method, params):
        requests.append((method, params))
        assert len(requests) == 1
        return page

    monkeypatch.setattr(app, "request", request)
    result = asyncio.run(app.manager_mcp_schema_status(schemas, declarations=schemas))
    assert requests == [
        (
            "mcpServerStatus/list",
            {
                "threadId": "private-synthetic-thread",
                "serverName": "autostopmanager",
                "detail": "toolsAndAuthOnly",
                "cursor": None,
            },
        )
    ]
    assert _json((schemas, changed, page)) == before
    assert "PRIVATE_ORIGIN" not in _json(result) and "private-synthetic-thread" not in _json(result)
    assert result["reload_guarantees_declarations"] is False
    assert result["ok"] is False
    assert result["diagnostic"] == "app_schema_stale_or_unavailable"
    assert result["app_schema_drift_tools"] == ["manager_automations"]
    assert result["declarations_match"] is True
