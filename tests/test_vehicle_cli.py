"""Exercise the standalone dispatch boundary without external provider requests."""

from __future__ import annotations

from functools import wraps
import importlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, get_type_hints

import httpx
import pytest

from autostop_manager import cli, vehicle_cli


VIN = "WVWZZZ1KZBW000001"
REQUESTS = {
    "inspect_vehicle_identifier": {"identifier": VIN, "identifier_type": "vin"},
    "decode_vin_vpic": {"identifier": VIN, "model_year": 2011, "timeout_seconds": 2.0, "extended": True},
    "decode_wmi_vpic": {"wmi": "WVW", "timeout_seconds": 2},
    "decode_wmi_local": {"wmi": "WVW"},
    "vininfo_decode": {"identifier": VIN},
    "corgi_decode": {"identifier": VIN, "model_year": 2011, "timeout_seconds": 4},
    "vin_brand_details": {"identifier": VIN, "context": {"make": "Volkswagen"}},
    "decode_frame_local": {"identifier": "NZE121-0000001"},
    "reconcile_vehicle_identity": {
        "identifier": VIN,
        "results": [],
        "context": {"make": "Volkswagen"},
        "detail": "summary",
    },
    "decode_vehicle_batch": {"items": [{"identifier": VIN}, "invalid"], "decoder": "decode_wmi_local"},
}


@pytest.mark.parametrize("name", REQUESTS)
@pytest.mark.parametrize("command", ["vehicle-tool", "e4"])
def test_dispatch_uses_current_function_and_preserves_its_response(name, command, monkeypatch, capsys):
    module = importlib.import_module("autostop_manager." + vehicle_cli.VEHICLE_TOOLS[name])
    original = getattr(module, name)
    calls = []
    response = {"ok": True, "outcome": "partial", "data": {"source": name}, "warnings": ["preserved"]}

    @wraps(original)
    def record(**arguments):
        calls.append(arguments)
        return response

    record.__annotations__ = get_type_hints(original)
    monkeypatch.setattr(module, name, record)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(REQUESTS[name])))
    assert cli.main([command, name, "--input", "-"]) == 0
    assert json.loads(capsys.readouterr().out) == response
    assert calls == [REQUESTS[name]]


@pytest.mark.parametrize(
    "name,payload",
    [
        ("decode_vin_vpic", {"identifier": VIN, "model_year": True}),
        ("decode_vin_vpic", {"identifier": VIN, "model_year": "2011"}),
        ("decode_vin_vpic", {"identifier": VIN, "timeout_seconds": "2"}),
        ("decode_vin_vpic", {"identifier": VIN, "extended": 1}),
        ("decode_vin_vpic", {"identifier": VIN, "extended": "false"}),
        ("decode_vin_vpic", {"identifier": 17}),
        ("decode_wmi_vpic", {"wmi": "WVW", "timeout_seconds": False}),
        ("decode_wmi_local", {"identifier": "WVW"}),
        ("vininfo_decode", {"identifier": VIN, "model_year": 2011}),
        ("reconcile_vehicle_identity", {"identifier": VIN, "results": ["not_an_object"]}),
        ("reconcile_vehicle_identity", {"identifier": VIN, "results": [], "detail": "raw"}),
        ("reconcile_vehicle_identity", {"identifier": VIN, "results": [], "context": []}),
        ("decode_vehicle_batch", {"items": [False], "decoder": "decode_vin_vpic"}),
        ("decode_vehicle_batch", {"items": [], "decoder": "decode_vin_vpic", "deadline_seconds": "30"}),
        ("decode_frame_local", {"identifier": "NZE121-0000001", "database_path": "/private"}),
    ],
)
def test_invalid_parameters_stop_before_selected_backend(name, payload, monkeypatch):
    module = importlib.import_module("autostop_manager." + vehicle_cli.VEHICLE_TOOLS[name])
    original = getattr(module, name)
    calls = []

    @wraps(original)
    def record(**arguments):
        calls.append(arguments)
        raise AssertionError("must_not_dispatch")

    record.__annotations__ = get_type_hints(original)
    monkeypatch.setattr(module, name, record)
    response = vehicle_cli.run_vehicle_tool(name, io.StringIO(json.dumps(payload)))
    assert response["outcome"] == "invalid_input"
    assert calls == []
    assert VIN not in json.dumps(response)


@pytest.mark.parametrize("payload", ["null", "[]", "1", '"text"'])
def test_request_requires_object(payload, monkeypatch):
    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: pytest.fail("unexpected_backend_load"))
    assert (
        vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.StringIO(payload))["outcome"] == "invalid_input"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{",
        b"{} {}",
        b"\xff",
        b'{"identifier":"a","identifier":"b"}',
        b'{"context":{"make":"a","make":"b"}}',
        b'{"identifier":NaN}',
        b'{"identifier":Infinity}',
        b'{"context":{"power":1e400}}',
        b'{"identifier":"\\ud800"}',
        b'{"context":{"\\udfff":"value"}}',
        b"[" * 1200 + b"0" + b"]" * 1200,
    ],
)
def test_malformed_or_ambiguous_json_never_loads_backend(payload, monkeypatch):
    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: pytest.fail("unexpected_backend_load"))
    response = vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(payload))
    assert response == {"ok": False, "outcome": "invalid_json", "error": "invalid_json", "data": {}}


def test_stdin_read_is_bounded_and_oversize_request_is_rejected(monkeypatch):
    requested = []

    class Oversize(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            requested.append(size)
            return super().read(size)

    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: pytest.fail("unexpected_backend_load"))
    response = vehicle_cli.run_vehicle_tool(
        "inspect_vehicle_identifier", Oversize(b" " * (vehicle_cli.MAX_REQUEST_BYTES + 2))
    )
    assert response["outcome"] == "request_too_large"
    assert requested == [vehicle_cli.MAX_REQUEST_BYTES + 1]


def test_size_limit_counts_utf8_bytes_for_text_stream(monkeypatch):
    monkeypatch.setattr(vehicle_cli, "MAX_REQUEST_BYTES", 32)
    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: pytest.fail("unexpected_backend_load"))
    assert (
        vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.StringIO('"' + "ж" * 16 + '"'))["outcome"]
        == "request_too_large"
    )


def test_exact_size_limit_is_accepted(monkeypatch):
    response = {"ok": True}
    monkeypatch.setattr(vehicle_cli, "call_vehicle_tool", lambda _name, _arguments: response)
    monkeypatch.setattr(vehicle_cli, "MAX_REQUEST_BYTES", 2)
    assert vehicle_cli.run_vehicle_tool("inspect_vehicle_identifier", io.BytesIO(b"{}")) is response


def test_json_depth_limit_is_enforced_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(vehicle_cli, "MAX_JSON_DEPTH", 3)
    monkeypatch.setattr(vehicle_cli, "call_vehicle_tool", lambda _name, value: calls.append(value) or {"ok": True})
    allowed = '{"context":{"options":["known"]}}'
    rejected = '{"context":{"options":[["known"]]}}'
    assert vehicle_cli.run_vehicle_tool("vin_brand_details", io.StringIO(allowed))["ok"] is True
    assert vehicle_cli.run_vehicle_tool("vin_brand_details", io.StringIO(rejected))["outcome"] == "invalid_json"
    assert calls == [json.loads(allowed)]


def test_unknown_tools_do_not_read_input_or_import_backend(monkeypatch):
    class Unreadable(io.StringIO):
        def read(self, size: int = -1) -> str:
            pytest.fail("unexpected_stdin_read")

    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: pytest.fail("unexpected_backend_load"))
    for name in ("store_management_action", "autostop_manager.storage.StoreState", VIN):
        response = vehicle_cli.run_vehicle_tool(name, Unreadable())
        assert response["outcome"] == "unknown_tool"
        assert name not in json.dumps(response)


def test_provider_exception_is_sanitized_and_not_retried(monkeypatch):
    calls = []

    def failing(identifier: str) -> dict[str, Any]:
        calls.append(identifier)
        raise RuntimeError("provider-secret-" + identifier)

    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: failing)
    response = vehicle_cli.run_vehicle_tool("decode_vin_vpic", io.StringIO(json.dumps({"identifier": VIN})))
    assert response["outcome"] == "tool_failed"
    assert calls == [VIN]
    assert VIN not in json.dumps(response)
    assert "provider-secret" not in json.dumps(response)


@pytest.mark.parametrize("response", [None, {"bad": b"private"}, {"bad": float("nan")}, {"bad": "\ud800"}])
def test_malformed_provider_result_is_a_safe_cli_error(response, monkeypatch):
    def provider(identifier: str) -> dict[str, Any]:
        return response

    monkeypatch.setattr(vehicle_cli, "_load_tool", lambda _name: provider)
    result = vehicle_cli.run_vehicle_tool("decode_vin_vpic", io.StringIO(json.dumps({"identifier": VIN})))
    assert result["outcome"] == "tool_failed"
    assert "private" not in json.dumps(result)


def test_local_tools_and_missing_optional_runtime_do_not_create_db_or_fallback(tmp_path, monkeypatch):
    from autostop_manager import automotive_identity, automotive_offline

    db = tmp_path / "manager.sqlite3"
    missing_runtime = tmp_path / "not-prepared"
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(db))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(missing_runtime))

    def denied(*args, **kwargs):
        pytest.fail("unexpected_database_or_network")

    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(httpx.Client, "request", denied)
    monkeypatch.setattr(httpx.AsyncClient, "request", denied)
    monkeypatch.setattr(automotive_identity.vin_lookup, "decode_vin_vpic", denied)
    monkeypatch.setattr(automotive_identity.vin_lookup, "decode_wmi_vpic", denied)
    monkeypatch.setattr(automotive_offline.subprocess, "run", denied)
    for name in (
        "inspect_vehicle_identifier",
        "decode_wmi_local",
        "vin_brand_details",
        "decode_frame_local",
        "reconcile_vehicle_identity",
        "vininfo_decode",
        "corgi_decode",
    ):
        response = vehicle_cli.run_vehicle_tool(name, io.StringIO(json.dumps(REQUESTS[name])))
        assert isinstance(response, dict)
        assert response["outcome"] != "tool_failed"
        if name in {"vininfo_decode", "corgi_decode"}:
            assert response["outcome"] == "dependency_missing"
    assert not db.exists()
    assert not missing_runtime.exists()
    assert list(tmp_path.iterdir()) == []


def test_import_and_parser_leave_manager_infrastructure_unloaded(tmp_path):
    source = Path(__file__).resolve().parents[1]
    script = """
import sys
from autostop_manager import cli
cli.build_parser().parse_args(['vehicle-tool', 'inspect_vehicle_identifier'])
assert 'autostop_manager.diagnostics' not in sys.modules
assert 'autostop_manager.mcp_probe' not in sys.modules
assert 'autostop_manager.storage' not in sys.modules
assert 'autostop_manager.automotive_identity' not in sys.modules
assert 'autostop_manager.automotive_offline' not in sys.modules
assert 'mcp' not in sys.modules
"""
    completed = subprocess.run([sys.executable, "-c", script], cwd=source, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_cli_safe_error_is_json_and_returns_failure(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    assert cli.main(["vehicle-tool", "inspect_vehicle_identifier"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["outcome"] == "invalid_json"
    assert captured.err == ""
