"""Negative scalar checks remain failures when a caller aggregates them into arrays."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, storage
from autostop_manager.crm_compat import register_crm_workflow_tools
from autostop_manager.storage import StoreState


@pytest.fixture(scope="module")
def event_loop():
    # Windows needs its internal socket pair before network operations are denied.
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(params=["direct", "native-crm"])
def ledger(request, tmp_path, monkeypatch, event_loop):
    database = tmp_path / "synthetic-workflow.sqlite3"
    connect = sqlite3.connect

    def isolated_connect(path, *args, **kwargs):
        assert Path(path).resolve() == database.resolve(), "only the synthetic SQLite fixture is permitted"
        return connect(path, *args, **kwargs)

    def denied(*_args, **_kwargs):
        raise AssertionError("workflow verification must not access external services or the runtime database")

    monkeypatch.setattr(sqlite3, "connect", isolated_connect)
    monkeypatch.setattr(config, "get_db_path", denied)
    monkeypatch.setattr(storage, "get_db_path", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)

    state = StoreState(database)
    run_id = state.start_workflow_run(
        workflow_id="synthetic-scalar-checks", intent="synthetic-scalar-checks", idempotency_key="synthetic-only"
    )["id"]
    assert state.transition_workflow_run(run_id, status="executing")["ok"]
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    server = FastMCP("synthetic-workflow-scalar-verification")
    register_crm_workflow_tools(server, state, {"workflow_transition"})
    tool = server._tool_manager.get_tool("workflow_transition")

    def complete(verification):
        arguments = {"run_id": run_id, "status": "completed", "verification": verification}
        before = deepcopy(arguments)
        if request.param == "direct":
            result = state.transition_workflow_run(**arguments)
        else:
            content, envelope = event_loop.run_until_complete(tool.run(arguments, convert_result=True))
            assert json.loads(content[0].text) == envelope
            result = envelope["summary"]
            assert envelope["ok"] == result["ok"]
        assert arguments == before
        return result

    return state, run_id, complete


@pytest.mark.parametrize("replay", [False, True], ids=["first-completion", "completed-replay"])
@pytest.mark.parametrize(
    ("verification", "paths"),
    [
        ({"readback": [False]}, ["readback[0]"]),
        ({"readback": ["failed"]}, ["readback[0]"]),
        ({"checks": [True, False]}, ["checks[1]"]),
        ({"verification": [[True], [False]]}, ["verification[1][0]"]),
        ({"executor": [True, {"ok": True}, ["passed", " NOT PASSED "]]}, ["executor[2][1]"]),
        ({"readback": {"ok": True, "checks": [False]}}, ["readback.checks[0]"]),
        (
            {"readback": {"ok": True, "business": {"verification": [False]}}},
            ["readback.business.verification[0]"],
        ),
        (
            {"readback": [False, {"ok": False}, [True, "rejected"]]},
            ["readback[0]", "readback[1].ok", "readback[2][1]"],
        ),
        ({"readback-results": [" BLOCKED "]}, ["readback_results[0]"]),
        ({"execution": [[["false"]]]}, ["execution[0][0][0]"]),
    ],
)
def test_negative_scalar_check_blocks_completion_or_replay_without_writing(ledger, verification, paths, replay):
    state, run_id, complete = ledger
    if replay:
        assert complete({"readback": {"ok": True}})["ok"]
    before = state.get_manager_run(run_id)["item"]

    rejected = complete(verification)

    assert rejected["ok"] is False
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == paths
    assert state.get_manager_run(run_id)["item"] == before


@pytest.mark.parametrize(
    "verification",
    [
        {"readback": [True, "completed", {"ok": True, "external_ref": "synthetic"}]},
        {"executor_ok": True, "optional_metadata": [False, "failed"]},
        {"readback": {"ok": True, "business": [False, "failed", {"paid": False}]}},
        {"readback": [{"ok": True, "business": [[False, "failed"]]}]},
        {"readback": {"ok": True, "business": {"nested": [False, "rejected"]}}},
        # Preserve the existing distinction between named checks and domain flags.
        {"readback": [{"paid": False, "available": False}], "executor_ok": True},
    ],
)
def test_successful_checks_and_business_payload_keep_existing_behavior(ledger, verification):
    state, run_id, complete = ledger
    before = state.get_manager_run(run_id)["item"]

    completed = complete(verification)

    assert completed["ok"] is True
    after = state.get_manager_run(run_id)["item"]
    assert after["status"] == "completed"
    assert after["state_version"] == before["state_version"] + 1
    assert len(after["events"]) == len(before["events"]) + 1
    assert after["verification"] == verification
    assert complete(verification)["deduplicated"] is True
    assert state.get_manager_run(run_id)["item"] == after


@pytest.mark.parametrize("verification", [{"readback": False}, {"readback": "failed"}])
def test_direct_negative_markers_keep_existing_paths(ledger, verification):
    state, run_id, complete = ledger
    before = state.get_manager_run(run_id)["item"]
    rejected = complete(verification)
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == ["readback"]
    assert state.get_manager_run(run_id)["item"] == before
