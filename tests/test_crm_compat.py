"""Contracts requested by CRM's selective sibling-project registrar; no CRM I/O."""

from __future__ import annotations

import inspect

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import StoreState

# Matches minimal_kanban.mcp.agent_gateway_support.MANAGER_GATEWAY_DEPENDENCY_NAMES.
CRM_DEPENDENCIES = frozenset(
    {
        "agent_bootstrap",
        "list_agent_workflows",
        "prepare_action_contract",
        "start_workflow",
        "workflow_status",
        "workflow_transition",
        "workflow_checkpoint",
        "workflow_wait_for_external",
        "complete_external_step",
        "workflow_resume",
        "workflow_cancel",
        "store_runtime_status",
        "store_digest",
        "store_search",
        "store_entity_context",
        "store_management_action",
        "store_quote_conductor",
        "download_store_quote_vin_photo",
        "store_owner_capabilities",
        "store_owner_api",
        "resolve_vin_oem_parts",
    }
)


@pytest.fixture
def crm(tmp_path):
    state = StoreState(tmp_path / "crm.sqlite3")
    server = FastMCP("synthetic-crm")
    register_manager_memory_tools(server, store=state, include_tools=CRM_DEPENDENCIES)
    return server, state


def test_crm_import_signature_and_selective_registration(crm, tmp_path):
    server, _ = crm
    inspect.signature(register_manager_memory_tools).bind(server, include_tools=CRM_DEPENDENCIES)
    assert set(server._tool_manager._tools) == CRM_DEPENDENCIES
    assert not (tmp_path / "crm.sqlite3").exists()
    plain = FastMCP("native-compatible")
    register_manager_memory_tools(plain, store=StoreState(tmp_path / "plain.sqlite3"))
    assert len(plain._tool_manager._tools) == 40
    assert "agent_bootstrap" not in plain._tool_manager._tools
    limited = FastMCP("limited")
    register_manager_memory_tools(limited, include_tools={"workflow_status"})
    assert set(limited._tool_manager._tools) == {"workflow_status"}


def test_workflow_discovery_uses_live_registration_not_keyword_routes(crm):
    server, _ = crm

    def call(name, **kw):
        return server._tool_manager._tools[name].fn(**kw)

    assert call("list_agent_workflows")["summary"]["workflow_count"] == 1

    @server.tool(name="agent_board_workflow")
    def board():
        return {"ok": True}

    listed = call("list_agent_workflows", query="CRM карточки", limit=1)
    assert listed["summary"]["workflow_count"] == 2
    assert listed["summary"]["selected_workflows"] == []
    assert listed["page"]["has_more"]
    selected = call("list_agent_workflows", intent="agent_board_workflow")
    assert selected["summary"]["selected_workflows"] == [
        {
            "workflow_id": "agent_board_workflow",
            "intent": "agent_board_workflow",
            "tool": "agent_board_workflow",
        }
    ]


def test_crm_ledger_lifecycle_preserves_guards_and_independent_readback(crm):
    server, state = crm

    def call(name, **kw):
        return server._tool_manager._tools[name].fn(**kw)

    arguments = {"workflow_id": "crm:synthetic", "intent": "synthetic", "idempotency_key": "synthetic-1"}
    started = call("start_workflow", **arguments)
    assert started["ok"] and started["status"] == "planned"
    run_id = started["run_id"]
    assert call("start_workflow", **arguments)["summary"]["deduplicated"]
    active = call("agent_bootstrap", query="not retained", limit=8)
    assert active["ok"] and active["summary"]["unfinished_runs"][0]["run_id"] == run_id
    assert "agent_mode" not in active["summary"]

    def version():
        return state.get_manager_run(run_id)["item"]["state_version"]

    assert call("workflow_resume", run_id=run_id, expected_state_version=version())["ok"]
    assert not call("workflow_checkpoint", run_id=run_id, checkpoint={}, expected_state_version=0)["ok"]
    assert not call(
        "workflow_checkpoint", run_id=run_id, checkpoint={"body": "forbidden"}, expected_state_version=version()
    )["ok"]
    assert call(
        "workflow_checkpoint", run_id=run_id, checkpoint={"next_action": "readback"}, expected_state_version=version()
    )["ok"]
    waiting = call(
        "workflow_wait_for_external",
        run_id=run_id,
        step_id="synthetic-step",
        connector="gmail",
        action="send_draft",
        request_refs={"draft_id": "synthetic-draft"},
        expected_state_version=version(),
    )
    assert waiting["ok"]
    assert not call("workflow_resume", run_id=run_id, expected_state_version=version())["ok"]
    assert not call(
        "complete_external_step",
        run_id=run_id,
        step_id="synthetic-step",
        result_refs={"body": "forbidden"},
        expected_state_version=version(),
    )["ok"]
    assert call(
        "complete_external_step",
        run_id=run_id,
        step_id="synthetic-step",
        result_refs={"message_id": "synthetic-message"},
        expected_state_version=version(),
    )["ok"]
    assert call("workflow_resume", run_id=run_id, expected_state_version=version())["ok"]
    assert call("workflow_transition", run_id=run_id, status="verifying", expected_state_version=version())["ok"]
    assert not call(
        "workflow_transition",
        run_id=run_id,
        status="completed",
        verification={"ok": False},
        expected_state_version=version(),
    )["ok"]
    assert call(
        "workflow_transition",
        run_id=run_id,
        status="completed",
        verification={"ok": True},
        expected_state_version=version(),
    )["ok"]
    verified = state.get_manager_run(run_id)["item"]
    assert verified["status"] == "completed"
    assert verified["external_steps"][0]["result_refs"] == {"message_id": "synthetic-message"}
    assert call("workflow_status", run_id=run_id, include_events=True)["summary"]["status"] == "completed"
    assert not call("workflow_status", run_id=999)["ok"]
    assert not call("workflow_cancel", run_id=run_id, expected_state_version=version())["ok"]
    assert call("agent_bootstrap")["summary"]["unfinished_runs"] == []
    second = call("start_workflow", **{**arguments, "idempotency_key": "synthetic-2"})
    assert call(
        "workflow_cancel",
        run_id=second["run_id"],
        reason="synthetic cancellation",
        expected_state_version=second["summary"]["state_version"],
    )["ok"]


def test_bootstrap_propagates_ledger_failure(crm, monkeypatch):
    server, _state = crm
    monkeypatch.setattr(
        StoreState, "list_active_manager_runs", lambda self, **kw: {"ok": False, "error": "synthetic_unavailable"}
    )
    result = server._tool_manager._tools["agent_bootstrap"].fn()
    assert not result["ok"] and result["status"] == "failed"
    assert result["warnings"] == ["synthetic_unavailable"]
