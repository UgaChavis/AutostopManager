from __future__ import annotations

import asyncio
import json

import pytest

from autostop_manager import store_analytics
from test_store_analytics import _Response, _payload


def _set_metric(payload, path, value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _report(raw, *, native, monkeypatch, tmp_path):
    monkeypatch.setattr(store_analytics, "urlopen", lambda *_args, **_kwargs: _Response(raw))
    if not native:
        return store_analytics.get_store_analytics_report(
            api_url="http://127.0.0.1:8765/internal/agent/v1", read_token="synthetic-token"
        ), None

    from mcp.server.fastmcp import FastMCP

    from autostop_manager import mcp_tools
    from autostop_manager.mcp_store_executor import StoreWorkers
    from autostop_manager.mcp_telemetry import instrument_manager_tools
    from autostop_manager.storage import StoreState

    monkeypatch.setattr(mcp_tools, "get_store_api_url", lambda: "http://127.0.0.1:8765/internal/agent/v1")
    monkeypatch.setattr(mcp_tools, "get_store_read_token", lambda: "synthetic-token")
    for name in ("get_store_manage_token", "get_store_quote_token", "get_store_owner_token"):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    server = FastMCP("synthetic-analytics-numbers")
    server.store_workers = StoreWorkers()
    mcp_tools.register_manager_tools(
        server, store=StoreState(tmp_path / "unused.sqlite3"), include_tools={"get_store_analytics_report"}
    )
    instrument_manager_tools(server)
    try:
        content, structured = asyncio.run(server.call_tool("get_store_analytics_report", {}))
        return structured, content[0].text
    finally:
        server.store_workers.close()


def _assert_serializable(result, native_text):
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    if native_text is not None:

        def reject_constant(value):
            pytest.fail(f"Non-JSON numeric constant in native output: {value}")

        assert json.loads(native_text, parse_constant=reject_constant) == result


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e309", "-1e309"])
@pytest.mark.parametrize(
    "path",
    [
        ("summary", "averageEngagedSeconds"),
        ("funnel", "viewToOrderRate"),
        ("comparison", "visitors", "percentChange"),
        ("topPages", 0, "views"),
        ("topProducts", 0, "views"),
        ("topProducts", 0, "cartAdditions"),
        ("clicks", 0, "count"),
        ("meta", "retentionDays"),
    ],
)
def test_analytics_rejects_nonfinite_metric(tmp_path, monkeypatch, native, token, path):
    payload = _payload()
    _set_metric(payload, path, "synthetic-numeric-token")
    raw = json.dumps(payload).replace('"synthetic-numeric-token"', token).encode()

    result, native_text = _report(raw, native=native, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert result["ok"] is False
    assert result["error"] == {"code": "store_analytics_response_invalid"}
    assert "summary" not in result
    _assert_serializable(result, native_text)
    if native:
        assert result["tool_execution"]["outcome"] == "error"


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("value", [0, 1, 0.125, 1e-300, 1e308, 10**400, -1, -0.125, -1e308])
def test_analytics_preserves_finite_numbers(tmp_path, monkeypatch, native, value):
    payload = _payload()
    # Signed deltas are valid even though counts and elapsed time cannot be negative.
    _set_metric(payload, ("comparison", "visitors", "delta"), value)
    if isinstance(value, int) and value >= 0:
        _set_metric(payload, ("topPages", 0, "views"), value)
    raw = json.dumps(payload, allow_nan=False).encode()

    result, native_text = _report(raw, native=native, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert result["ok"] is True
    assert result["comparison"]["visitors"]["delta"] == value
    if isinstance(value, int) and value >= 0:
        assert result["topPages"][0]["views"] == value
    _assert_serializable(result, native_text)
