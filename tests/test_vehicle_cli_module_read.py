"""Selected module I/O failures keep the standalone CLI response usable."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import cli, vehicle_cli

ARGUMENTS = b'{"identifier":"DEMO"}'
FAILURE = {"ok": False, "outcome": "tool_failed", "error": "tool_failed", "data": {}}


def import_failure(monkeypatch, error):
    attempts = []

    def load(name, *, package):
        attempts.append((name, package))
        raise error

    # Replace only this module's loader namespace, leaving Python imports alone.
    monkeypatch.setattr(vehicle_cli, "importlib", SimpleNamespace(import_module=load))
    return attempts


@pytest.mark.parametrize("command", ["vehicle-tool", "e4"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, FileNotFoundError])
def test_module_read_failure_prints_one_safe_json_result_and_exits_one(monkeypatch, capsys, command, error_type):
    attempts = import_failure(monkeypatch, error_type("synthetic unreadable module marker"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(ARGUMENTS)))
    assert cli.main([command, "inspect_vehicle_identifier"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == FAILURE
    assert output.err == ""
    assert "synthetic unreadable module marker" not in output.out
    assert attempts == [(".automotive_identity", "autostop_manager")]


def test_direct_dispatch_also_returns_safe_module_read_failure(monkeypatch):
    attempts = import_failure(monkeypatch, PermissionError("synthetic unreadable module marker"))
    assert vehicle_cli.call_vehicle_tool("inspect_vehicle_identifier", {"identifier": "DEMO"}) == FAILURE
    assert attempts == [(".automotive_identity", "autostop_manager")]


def test_later_invocation_can_use_healthy_selected_backend_without_retry(monkeypatch):
    attempts = []
    calls = []

    def backend(identifier: str) -> dict[str, Any]:
        calls.append(identifier)
        return {"ok": True, "demo": True}

    def load(name, *, package):
        attempts.append((name, package))
        if len(attempts) == 1:
            raise PermissionError("synthetic unreadable module marker")
        return SimpleNamespace(inspect_vehicle_identifier=backend)

    monkeypatch.setattr(vehicle_cli, "importlib", SimpleNamespace(import_module=load))
    assert vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(ARGUMENTS)) == FAILURE
    assert len(attempts) == 1
    assert calls == []
    assert vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(ARGUMENTS)) == {
        "ok": True,
        "demo": True,
    }
    assert attempts == [(".automotive_identity", "autostop_manager")] * 2
    assert calls == ["DEMO"]


@pytest.mark.parametrize("error_type", [ImportError, AttributeError, RuntimeError])
def test_existing_module_load_errors_keep_their_response(monkeypatch, error_type):
    attempts = import_failure(monkeypatch, error_type("synthetic module load failure"))
    assert vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(ARGUMENTS)) == FAILURE
    assert attempts == [(".automotive_identity", "autostop_manager")]


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_module_load_does_not_swallow_process_cancellation(monkeypatch, error_type):
    error = error_type("synthetic cancellation")
    attempts = import_failure(monkeypatch, error)
    with pytest.raises(error_type) as caught:
        vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(ARGUMENTS))
    assert caught.value is error
    assert attempts == [(".automotive_identity", "autostop_manager")]
