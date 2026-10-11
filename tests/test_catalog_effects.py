"""Catalog effects are checked against native execution with isolated HTTP and state."""

from __future__ import annotations

import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_tools, store_analytics, vehicle_identity_transport
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient

ROOT = Path(__file__).resolve().parents[1]
NATIVE_NAMES = (
    "benchmark_vin_parts_lookup",
    "download_store_quote_vin_photo",
    "get_store_analytics_report",
    "store_digest",
    "store_owner_capabilities",
    "store_runtime_status",
)
SYNTHETIC_VIN = "1HGCM826300000001"
SOURCE_PHOTO_SHA256 = "c" * 64
PREVIEW = b"\xff\xd8synthetic bounded preview\xff\xd9"


class _Response(io.BytesIO):
    def __init__(self, payload, *, content_type="application/json"):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.headers = {"Content-Type": content_type}
        self.status = 200


def _envelope():
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {"snapshot_at": "2026-10-11T00:00:00Z"},
        "items": [],
        "changes": [],
        "page": {"has_more": False, "next_cursor": "synthetic-cursor", "limit": 25},
        "warnings": [],
        "meta": {"snapshot_at": "2026-10-11T00:00:00Z"},
    }


def _analytics_payload():
    return {
        "ok": True,
        "schema": store_analytics.STORE_ANALYTICS_FORMAT,
        "timezone": "Asia/Krasnoyarsk",
        "period": {"start": "2026-10-11T00:00:00+07:00", "end": "2026-10-11T23:59:59+07:00"},
        "previousPeriod": {"start": "2026-10-10T00:00:00+07:00", "end": "2026-10-10T23:59:59+07:00"},
        "summary": dict.fromkeys(store_analytics._SUMMARY_FIELDS, 0),
        "funnel": dict.fromkeys(store_analytics._FUNNEL_FIELDS, 0),
        "topPages": [],
        "topProducts": [],
        "clicks": [],
        "comparison": {},
        "meta": {"aggregatedOnly": True, "rawEventsIncluded": False, "retentionDays": 60},
    }


@pytest.fixture
def isolated_http(monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real network is forbidden in catalog effect tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(vehicle_identity_transport, "_PROVIDER_CIRCUIT", vehicle_identity_transport._ProviderCircuit())

    def respond(request, **_kwargs):
        calls.append(request)
        path = urlsplit(request.full_url).path
        if path.endswith("/vin-photo-preview"):
            return _Response(PREVIEW, content_type="image/jpeg")
        if path.endswith("/openapi.json"):
            return _Response(
                {
                    "paths": {
                        f"/api/v1/synthetic-parts/{index}": {
                            "get": {"operationId": f"list_synthetic_parts_{index}", "responses": {}}
                        }
                        for index in range(100)
                    }
                }
            )
        if path.endswith(store_analytics.STORE_ANALYTICS_PATH):
            return _Response(_analytics_payload())
        if "DecodeWMI" in path:
            return _Response({"Results": [{"WMI": "1HG", "Name": "Synthetic manufacturer"}]})
        if "DecodeVinValues" in path:
            return _Response(
                {"Results": [{"VIN": SYNTHETIC_VIN, "Make": "HONDA", "Model": "Accord", "ErrorCode": "0"}]}
            )
        if path.endswith(("/digest", "/runtime-status", "/bootstrap-snapshot")):
            payload = _envelope()
            if not path.endswith("/digest"):
                payload["summary"] = {}
                payload["page"]["next_cursor"] = None
            return _Response(payload)
        raise AssertionError(f"unexpected HTTP route in catalog test: {path}")

    for target in (
        "autostop_manager.store_api.urlopen",
        "autostop_manager.store_owner_api._open_response",
        "autostop_manager.store_analytics.urlopen",
        "autostop_manager.vin_lookup.urlopen",
    ):
        monkeypatch.setattr(target, respond)
    return calls


@pytest.fixture
def native_tools(tmp_path, monkeypatch, isolated_http):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    api_url = "http://127.0.0.1:8010/internal/agent/v1"
    monkeypatch.setattr(mcp_tools, "get_store_api_url", lambda: api_url)
    monkeypatch.setattr(mcp_tools, "get_store_read_token", lambda: "synthetic-read-token")
    monkeypatch.setattr(mcp_tools, "get_store_owner_token", lambda: "synthetic-owner-token")
    store = StoreState(tmp_path / "catalog-effects.sqlite3")
    store.initialize()
    client = StoreApiClient(
        api_url=api_url,
        read_token="synthetic-read-token",
        manage_token="",
        quote_token="synthetic-quote-token",
        max_read_attempts=1,
    )
    server = FastMCP("catalog-effects-test")
    mcp_tools.register_manager_tools(server, store, store_client=client, include_tools=NATIVE_NAMES)
    return {name: tool.fn for name, tool in server._tool_manager._tools.items()}, store


def _snapshot(store):
    with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as connection:
        return tuple(connection.iterdump())


def _catalog_row(name):
    registry = json.loads((ROOT / "docs/agent/automotive_tools.json").read_text())
    return next(row for row in registry["tools"] if row["tool_id"] == "manager." + name)


def _assert_effects_compatible(row, *, http_calls, state_changed):
    kind = row["execution_kind"]
    if http_calls:
        assert kind not in {"pure", "local_read"}, f"{row['tool_id']} declares {kind} but dispatched HTTP"
    if state_changed:
        assert kind in {"composed", "job_write", "business_write"}, (
            f"{row['tool_id']} declares {kind} but wrote Manager state"
        )


def _exercise(name, native_tools):
    tools, store = native_tools
    before = _snapshot(store)
    if name == "benchmark_vin_parts_lookup":
        result = tools[name](
            [{"identifier": SYNTHETIC_VIN}],
            requested_part="передние колодки",
            use_vpic_batch=False,
            include_partsapi_dry_run=False,
        )
    elif name == "download_store_quote_vin_photo":
        result = tools[name](quote_request_id="synthetic-quote", expected_photo_sha256=SOURCE_PHOTO_SHA256)
    elif name == "store_runtime_status":
        result = tools[name](live=True)
    else:
        result = tools[name]()
    assert result["ok"] is True
    return result, _snapshot(store) != before


@pytest.mark.parametrize("name", NATIVE_NAMES)
def test_catalog_declares_effects_observed_through_real_native_facades(name, native_tools, isolated_http):
    result, changed = _exercise(name, native_tools)
    assert isolated_http, "scenario must actually dispatch mocked HTTP"
    row = _catalog_row(name)
    _assert_effects_compatible(row, http_calls=isolated_http, state_changed=changed)
    if name == "benchmark_vin_parts_lookup":
        assert row["execution_kind"] == "composed"
        assert result["summary"]["part_intent_recognized_count"] == 1
        assert "vPIC" in row["primary_data_source"]
        assert "PartsAPI" in row["primary_data_source"]
    else:
        assert row["provider_id"] == "store"
        assert row["primary_data_source"] != "AutoStop versioned rules or explicit supplied inputs"
        assert row["reference"] == "docs/agent/references/store-api.md"
    assert changed is (name == "store_digest")


@pytest.mark.parametrize("name", NATIVE_NAMES)
@pytest.mark.parametrize("false_kind", ["pure", "local_read"])
def test_effect_gate_rejects_false_pure_and_local_read_metadata(name, false_kind, native_tools, isolated_http):
    _, changed = _exercise(name, native_tools)
    assert isolated_http
    row = deepcopy(_catalog_row(name))
    row["execution_kind"] = false_kind
    with pytest.raises(AssertionError, match="dispatched HTTP"):
        _assert_effects_compatible(row, http_calls=isolated_http, state_changed=changed)


def test_digest_cannot_hide_checkpoint_writes_behind_network_read(native_tools, isolated_http):
    _, changed = _exercise("store_digest", native_tools)
    assert changed
    row = deepcopy(_catalog_row("store_digest"))
    row["execution_kind"] = "network_read"
    with pytest.raises(AssertionError, match="wrote Manager state"):
        _assert_effects_compatible(row, http_calls=isolated_http, state_changed=changed)


def test_runtime_default_is_local_but_live_and_bootstrap_dispatch_http(native_tools, isolated_http):
    tools, store = native_tools
    before = _snapshot(store)
    assert tools["store_runtime_status"]()["meta"]["live_check"] is False
    assert isolated_http == []
    assert tools["store_runtime_status"](live=True)["ok"] is True
    assert tools["store_runtime_status"](bootstrap_snapshot=True)["ok"] is True
    assert [urlsplit(call.full_url).path.rsplit("/", 1)[-1] for call in isolated_http] == [
        "runtime-status",
        "bootstrap-snapshot",
    ]
    assert _snapshot(store) == before


def test_owner_capabilities_reads_openapi_once_and_reuses_its_cache(native_tools, isolated_http):
    tools, store = native_tools
    before = _snapshot(store)
    first = tools["store_owner_capabilities"]()
    second = tools["store_owner_capabilities"]()
    assert first["ok"] is True and second["ok"] is True
    assert second["items"] == first["items"] and second["summary"] == first["summary"]
    assert first["data_included"] is False
    assert len(isolated_http) == 1 and urlsplit(isolated_http[0].full_url).path == "/openapi.json"
    assert _snapshot(store) == before


def test_store_photo_returns_base64_preview_and_distinct_source_hash_without_a_file(native_tools, isolated_http):
    tools, store = native_tools
    files_before = set(store.path.parent.iterdir())
    result = tools["download_store_quote_vin_photo"](
        quote_request_id="synthetic-quote", expected_photo_sha256=SOURCE_PHOTO_SHA256
    )
    assert result["ok"] is True and len(isolated_http) == 1
    assert base64.b64decode(result["data"]["content_base64"], validate=True) == PREVIEW
    attachment = result["summary"]["attachment"]
    assert attachment["sha256"] == SOURCE_PHOTO_SHA256
    assert attachment["preview_sha256"] == hashlib.sha256(PREVIEW).hexdigest()
    assert attachment["preview_sha256"] != attachment["sha256"]
    assert result["data"]["content_type"] == "image/jpeg"
    assert set(result["data"]) == {"content_base64", "content_type"}
    assert set(store.path.parent.iterdir()) == files_before


def test_benchmark_explicit_offline_mode_dispatches_no_http(native_tools, isolated_http):
    tools, store = native_tools
    before = _snapshot(store)
    result = tools["benchmark_vin_parts_lookup"](
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        include_partsapi_dry_run=False,
    )
    assert result["ok"] is True and result["summary"]["count"] == 1
    assert isolated_http == []
    assert _snapshot(store) == before
