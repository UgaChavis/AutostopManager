from __future__ import annotations

import asyncio
import json
import socket

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from autostop_manager import config as manager_config
from autostop_manager.automotive_mcp import register_automotive_tools
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState


@pytest.fixture(autouse=True)
def isolated_native_inputs(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("pure price calculation attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    for name in (
        "AUTOSTOP_STORE_API_URL",
        "AUTOSTOP_STORE_READ_TOKEN",
        "AUTOSTOP_STORE_MANAGE_TOKEN",
        "AUTOSTOP_STORE_OWNER_TOKEN",
        "AUTOSTOP_STORE_QUOTE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def native_server(tmp_path):
    def build(route):
        server = FastMCP("native-hourly-rate")
        if route == "bare":
            register_automotive_tools(server)
        else:
            assert route == "manager"
            register_manager_tools(
                server,
                StoreState(tmp_path / "technical.sqlite3"),
                include_tools={"calculate_work_price"},
            )
        return server

    return build


def arguments_for(rate, *, encoded=False):
    arguments = json.loads(
        json.dumps(
            {
                "hourly_rate": rate,
                "labor": [{"operation_id": "demo", "operation_name": "DEMO", "hours": 2}],
                "policy": {"basis": "hourly_rate", "version": "test-v1"},
            }
        )
    )
    if encoded:
        for name in ("labor", "policy"):
            arguments[name] = json.dumps(arguments[name])
    return arguments


def assert_public_schema(server):
    tool = server._tool_manager.get_tool("calculate_work_price")
    assert tool.parameters["properties"]["hourly_rate"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}],
        "default": None,
        "title": "Hourly Rate",
    }
    assert tool.fn_metadata.arg_model.model_json_schema(by_alias=True) == tool.parameters


@pytest.mark.parametrize("route", ["bare", "manager"])
@pytest.mark.parametrize("rate", [False, True], ids=["false", "true"])
def test_native_hourly_rate_rejects_raw_json_boolean(native_server, route, rate):
    server = native_server(route)
    assert_public_schema(server)
    arguments = arguments_for(rate)
    original = json.dumps(arguments, sort_keys=True)
    assert type(arguments["hourly_rate"]) is bool

    with pytest.raises(ToolError, match="hourly_rate"):
        asyncio.run(server.call_tool("calculate_work_price", arguments))

    assert json.dumps(arguments, sort_keys=True) == original


@pytest.mark.parametrize(
    "route,rate,total,encoded",
    [
        pytest.param("bare", 0, 0, False, id="zero"),
        pytest.param("bare", 1000.5, 2001, False, id="ordinary-number"),
        pytest.param("bare", "1000.5", 2001, False, id="legacy-numeric-string"),
        pytest.param("manager", 0, 0, True, id="manager-encoded-nested-zero"),
    ],
)
def test_native_hourly_rate_preserves_valid_inputs_and_receipts(native_server, route, rate, total, encoded):
    server = native_server(route)
    assert_public_schema(server)
    arguments = arguments_for(rate, encoded=encoded)
    original = json.dumps(arguments, sort_keys=True)

    content, response = asyncio.run(server.call_tool("calculate_work_price", arguments))

    assert json.loads(content[0].text) == response
    assert response["outcome"] == "success"
    assert response["data"]["hourly_rate"] == float(rate)
    assert response["data"]["total"] == total
    assert response["execution"]["network_calls"] == 0
    assert json.dumps(arguments, sort_keys=True) == original
