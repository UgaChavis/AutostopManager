"""Store-only registration supports native Windows without J1 process modules."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager.mcp_catalog_tools import register_store_owner_tools, register_store_read_tools


class OwnerClient:
    def __init__(self):
        self.calls = []
        self.preparations = []

    def list_capabilities(self, **kwargs):
        return {"ok": True, "arguments": kwargs}

    def prepare_invocation(self, operation_id, **kwargs):
        self.preparations.append(kwargs)
        method = "GET" if operation_id == "get_part" else "PATCH"
        return {
            "ok": True,
            "summary": {
                "operation_id": operation_id,
                "method": method,
                "path": "/api/v1/parts/{id}",
                "concrete_path": f"/api/v1/parts/{kwargs['path_parameters']['id']}",
                "risk": "read" if method == "GET" else "write",
                "path_parameters": ["id"],
                "schema_hash": "a" * 64,
                "query_sha256": "b" * 64,
                "request_sha256": "c" * 64,
                "plan_hash": "d" * 64,
                "query_fields": [],
                "verification_class": "exact_entity",
                "revision_required": method != "GET",
            },
        }

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "data": {"id": "part-1"}}


def test_portable_registration_keeps_four_native_tool_schemas():
    server = FastMCP("catalog-registration")
    register_store_owner_tools(server, OwnerClient())
    register_store_read_tools(server, SimpleNamespace())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"store_owner_api", "store_owner_capabilities", "store_search", "store_entity_context"}
    assert tools["store_owner_api"].inputSchema["required"] == ["operation_id"]
    assert "dry_run_proof" in tools["store_owner_api"].inputSchema["properties"]
    assert tools["store_search"].inputSchema["required"] == ["entity"]
    assert tools["store_entity_context"].inputSchema["required"] == ["entity", "entity_id"]


def test_portable_revision_requests_route_preparation_without_payload():
    server = FastMCP("catalog-revision")
    client = OwnerClient()
    register_store_owner_tools(server, client)
    tool = server._tool_manager.get_tool("store_owner_api").fn

    result = tool(operation_id="update_part", target_id="part-1", mode="revision", path_parameters={"id": "part-1"})

    assert result["ok"]
    assert client.preparations[0]["revision_only"] is True
    assert client.calls[0]["mode"] == "revision"


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
def test_portable_owner_registration_preserves_binding_and_contract_checks(mode):
    server = FastMCP("catalog-contract")
    client = OwnerClient()
    register_store_owner_tools(server, client)
    tool = server._tool_manager.get_tool("store_owner_api").fn
    arguments = {
        "operation_id": "update_part",
        "target_id": "part-1",
        "path_parameters": {"id": "part-1"},
        "body": {"name": "Синтетическая карточка"},
        "owner_intent": "Изменить синтетическую карточку",
        "idempotency_key": "catalog-portable-change-001",
        "correlation_id": "catalog-portable-flow-001",
        "expected_revision": "revision-1",
    }
    wrong_target = tool(mode=mode, **{**arguments, "target_id": "part-other"})
    assert wrong_target["error"]["code"] == "store_owner_target_binding_mismatch"
    prepare = tool(mode="prepare", prepare_for_mode=mode, **arguments)
    assert prepare["ok"] and prepare["meta"]["request_dispatched"] is False
    mismatch = tool(mode=mode, expected_contract_id="ac_" + "0" * 20, **arguments)
    assert mismatch["error"]["code"] == "store_owner_action_contract_mismatch"
    assert client.calls == []
    result = tool(mode=mode, expected_contract_id=prepare["meta"]["contract_id"], dry_run_proof="b" * 64, **arguments)
    assert result["ok"]
    assert client.calls[0]["dry_run_proof"] == "b" * 64
    assert client.calls[0]["expected_plan_hash"] == "d" * 64
