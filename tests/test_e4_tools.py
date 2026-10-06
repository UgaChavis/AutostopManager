from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx
from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import cli, e4_tools
from autostop_manager.e4_mcp import register_e4_tools
from autostop_manager.vehicle_identity_transport import IdentityBudget, collect_source_provider_results_async

VIN = "1HGCM82633A000000"


def test_registry_cli_does_not_import_store_or_mcp():
    script = (
        "import sys; from autostop_manager.cli import build_parser; build_parser(); "
        "assert 'autostop_manager.storage' not in sys.modules; assert 'mcp' not in sys.modules"
    )
    assert subprocess.run([sys.executable, "-c", script], capture_output=True).returncode == 0


@pytest.mark.parametrize(
    "payload", ["not json", "[]", '{"identifier": "private bad input"}', "x" * (5 * 1024 * 1024 + 1)]
)
def test_cli_invalid_requests_are_one_safe_json(monkeypatch, capsys, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert cli.main(["e4", "inspect_vehicle_identifier", "--input", "-"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["ok"] is False
    assert "private bad input" not in out.out
    assert out.err == ""


def test_cli_inspect_has_no_decoder_or_network(monkeypatch, capsys):
    def fail(*args, **kwargs):
        pytest.fail("Inspect invoked a source")

    for name in e4_tools.SOURCE_TOOLS:
        monkeypatch.setitem(e4_tools.E4_TOOLS, name, fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"identifier": VIN})))
    assert cli.main(["e4", "inspect_vehicle_identifier"]) == 0
    rendered = capsys.readouterr().out
    assert VIN not in rendered
    assert json.loads(rendered)["ok"]


@pytest.mark.parametrize("count", [0, 1, 500])
def test_batch_only_selected_source_preserves_order_and_duplicates(monkeypatch, count):
    calls = []

    def chosen(identifier):
        calls.append(identifier)
        return {"ok": True, "marker": len(calls)}

    monkeypatch.setitem(e4_tools.E4_TOOLS, "decode_wmi_local", chosen)
    rows = [{"identifier": "WBA"}] * count
    result = e4_tools.decode_vehicle_batch("decode_wmi_local", rows)
    assert len(calls) == count
    assert [row["item_index"] for row in result["results"]] == list(range(count))
    assert [row["marker"] for row in result["results"]] == list(range(1, count + 1))


def test_batch_rejects_recursive_tool_and_oversize_and_preserves_bad_rows(monkeypatch):
    assert e4_tools.decode_vehicle_batch("reconcile_vehicle_identity", [])["outcome"] == "unsupported_batch_tool"
    assert e4_tools.decode_vehicle_batch("decode_wmi_local", [{}] * 501)["outcome"] == "identity_batch_too_large"
    result = e4_tools.decode_vehicle_batch("decode_wmi_local", [{"identifier": "WVW"}, None, {}, {"identifier": 7}])
    assert result["count"] == 4
    assert result["results"][0]["ok"]
    assert all(row["outcome"] == "invalid_input" for row in result["results"][1:])


def test_mcp_surface_comes_from_same_registry_and_batch_enum():
    server = FastMCP("independent-e4")
    register_e4_tools(server)
    assert set(server._tool_manager._tools) == set(e4_tools.E4_TOOLS)
    schema = server._tool_manager._tools["decode_vehicle_batch"].parameters
    assert set(schema["properties"]["tool"]["enum"]) == e4_tools.SOURCE_TOOLS
    assert "ctx" not in schema["properties"]


def test_selected_vin_transport_does_not_schedule_wmi_or_fallback(monkeypatch):
    from autostop_manager import vehicle_identity_transport as transport

    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"Results": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await collect_source_provider_results_async(
                [{"identifier": VIN, "ok": True}], source="vin", budget=IdentityBudget(), client=client
            )

    result = asyncio.run(run())
    assert len(urls) == 1
    assert "DecodeVINValuesBatch" in urls[0]
    assert result["results"][0]["ok"] is False
    assert result["processing"]["http_attempts"] == 1


def test_source_wmi_preserves_six_characters_and_single_budget(monkeypatch):
    from autostop_manager import vehicle_identity_transport as transport

    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"Results": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await collect_source_provider_results_async(
                [{"identifier": "1A9000", "ok": True}] * 3,
                source="wmi",
                budget=IdentityBudget(max_attempts=2),
                client=client,
            )

    result = asyncio.run(run())
    assert len(urls) == 1 and "/1A9000?" in urls[0]
    assert len(result["results"]) == 3


def test_selected_transport_cancellation_does_not_leave_provider_tasks(monkeypatch):
    from autostop_manager import vehicle_identity_transport as transport

    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())

    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(
                collect_source_provider_results_async(
                    [{"identifier": VIN}], source="vin", budget=IdentityBudget(), client=client
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("malformed_type", [[], {}])
@pytest.mark.parametrize(
    "tool", ["inspect_vehicle_identifier", "decode_vin_vpic", "vin_brand_details", "decode_frame_local"]
)
def test_cli_malformed_identifier_type_is_safe_json(monkeypatch, capsys, tool, malformed_type):
    request = {"identifier": VIN, "identifier_type": malformed_type}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert cli.main(["e4", tool, "--input", "-"]) == 1
    rendered = capsys.readouterr()
    result = json.loads(rendered.out)
    assert result["outcome"] == "invalid_input"
    assert VIN not in rendered.out
    assert rendered.err == ""


def test_cli_malformed_batch_rows_preserve_valid_neighbours(monkeypatch, capsys):
    valid = {"identifier": "ES1-0000000", "identifier_type": "frame_number"}
    request = {
        "tool": "decode_frame_local",
        "items": [valid, {**valid, "identifier_type": []}, {**valid, "identifier_type": {}}, valid],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert cli.main(["e4", "decode_vehicle_batch", "--input", "-"]) == 1
    rendered = capsys.readouterr()
    result = json.loads(rendered.out)
    assert result["count"] == 4
    assert [row["item_index"] for row in result["results"]] == [0, 1, 2, 3]
    assert result["results"][0]["ok"] and result["results"][3]["ok"]
    assert [row["outcome"] for row in result["results"][1:3]] == ["invalid_input", "invalid_input"]
    assert valid["identifier"] not in rendered.out
    assert rendered.err == ""


@pytest.mark.parametrize("malformed_type", [[], {}])
def test_async_malformed_identifier_type_never_reaches_provider(monkeypatch, malformed_type):
    from autostop_manager import vehicle_identity_transport as transport

    async def fail(*args, **kwargs):
        pytest.fail("Invalid input reached the provider")

    monkeypatch.setattr(transport, "_request_async", fail)
    result = asyncio.run(
        e4_tools.call_e4_tool_async("decode_vin_vpic", {"identifier": VIN, "identifier_type": malformed_type})
    )
    assert result["outcome"] == "invalid_input"
    assert result["processing"]["http_attempts"] == 0


@pytest.mark.parametrize("mode", ["single", "native", "batch"])
def test_corgi_cancellation_reaps_worker_and_does_not_start_remaining_rows(monkeypatch, mode):
    from autostop_manager import e4_optional

    started = threading.Event()
    stopped = threading.Event()
    calls = []

    def decode(identifier, *, _cancel_event=None, **kwargs):
        calls.append(identifier)
        started.set()
        try:
            assert _cancel_event is not None
            assert _cancel_event.wait(timeout=3), "Worker did not receive cancellation"
            return {"ok": False, "outcome": "decoder_cancelled"}
        finally:
            stopped.set()

    monkeypatch.setattr(e4_optional, "corgi_decode", decode)

    async def run():
        disconnected = asyncio.Event()
        if mode == "native":

            async def is_disconnected():
                return disconnected.is_set()

            context = SimpleNamespace(
                request_context=SimpleNamespace(request=SimpleNamespace(is_disconnected=is_disconnected))
            )
            server = FastMCP("e4-cancellation")
            register_e4_tools(server)
            function = server._tool_manager._tools["corgi_decode"].fn
            operation = function(identifier=VIN, ctx=context)
        elif mode == "batch":
            operation = e4_tools.call_e4_tool_async(
                "decode_vehicle_batch", {"tool": "corgi_decode", "items": [{"identifier": VIN}] * 3}
            )
        else:
            operation = e4_tools.call_e4_tool_async("corgi_decode", {"identifier": VIN})
        task = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(started.wait, 2), "Worker did not start"
            if mode == "native":
                disconnected.set()
            else:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert stopped.is_set()
            assert calls == [VIN]
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
