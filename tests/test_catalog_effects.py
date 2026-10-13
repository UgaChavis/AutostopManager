"""Catalog effects are checked against native execution with isolated HTTP and state."""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import (
    automation_registry,
    j1_research,
    j1_vin_store,
    mcp_tools,
    store_analytics,
    vehicle_identity_transport,
)
from autostop_manager.automation_control import (
    AUTOMATION_CONTROL_PROTOCOL,
    AutomationControlClient,
    AutomationControlService,
)
from autostop_manager.automation_registry import AutomationStore
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


@pytest.fixture
def g1_native_tools(tmp_path, monkeypatch, isolated_http):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    registry = AutomationStore(tmp_path / "g1-registry.sqlite3")
    service = AutomationControlService(registry, timer_controller=SimpleNamespace(list_status=list))
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock[0])
    requests = []

    def request(_client, operation, payload=None, **options):
        requests.append(operation)
        command = {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": operation,
            "actor": {"kind": "codex", "id": "synthetic-catalog-review", "is_admin": True},
            "payload": payload or {},
        }
        for field in ("idempotency_key", "expected_revision"):
            if options.get(field) is not None:
                command[field] = options[field]
        return {"ok": True, "data": service.handle(command)}

    monkeypatch.setattr(AutomationControlClient, "request", request)
    server = FastMCP("g1-catalog-effects-test")
    mcp_tools.register_manager_tools(
        server,
        StoreState(tmp_path / "unused-manager.sqlite3"),
        include_tools=("manager_automations", "manager_automation_control"),
    )

    def invoke(name, **arguments):
        return asyncio.run(server._tool_manager.get_tool(name).run(arguments))

    tools = {name: partial(invoke, name) for name in server._tool_manager._tools}
    annotations = {name: tool.annotations for name, tool in server._tool_manager._tools.items()}
    return tools, registry, service, clock, requests, annotations


def _exercise_g1(operation, g1_native_tools):
    tools, registry, service, clock, requests, annotations = g1_native_tools
    registry.initialize()
    job = None
    if operation == "preview":
        command = {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": "create_from_template",
            "actor": {"kind": "codex", "id": "synthetic-catalog-review", "is_admin": True},
            "payload": {"template_id": "crm_digest_v1"},
            "idempotency_key": "synthetic-create-g1-0001",
        }
        job = service.handle(command)["job"]
    before = _snapshot(registry)
    clock[0] = datetime(2026, 1, 2, tzinfo=UTC)
    if operation == "preview":
        result = tools["manager_automation_control"](
            operation="preview",
            payload={
                "target_operation": "set_enabled",
                "target_payload": {"job_id": job["job_id"], "enabled": True},
            },
        )
    else:
        result = tools["manager_automations"](operation=operation)
    assert result["ok"] is True
    assert requests == [operation]
    changed = _snapshot(registry) != before
    assert changed, "native G1 route must demonstrate real registry metadata writes"
    with closing(sqlite3.connect(registry.path.as_uri() + "?mode=ro", uri=True)) as connection:
        schema = connection.execute("SELECT upgraded_at FROM manager_automation_schema").fetchone()
        assert schema == ("2026-01-02T00:00:00Z",)
        assert connection.execute("SELECT COUNT(*) FROM manager_automation_outbox").fetchone() == (0,)
        if job is not None:
            assert connection.execute("SELECT desired_state, revision FROM manager_automations").fetchone() == (
                "off",
                job["revision"],
            )
    if operation in {"status", "readiness", "preview"}:
        # Native annotations must cover the observed SQLite writes, including a
        # repeat with identical arguments after the schema metadata clock advances.
        tool_name = "manager_automation_control" if operation == "preview" else "manager_automations"
        hints = annotations[tool_name]
        assert hints.readOnlyHint is False
        assert hints.idempotentHint is False
        assert hints.destructiveHint is (operation == "preview")
        assert hints.openWorldHint is False
        first_state = _snapshot(registry)
        clock[0] = datetime(2026, 1, 3, tzinfo=UTC)
        repeat_payload = (
            {"target_operation": "set_enabled", "target_payload": {"job_id": job["job_id"], "enabled": True}}
            if job is not None
            else None
        )
        repeated = (
            tools[tool_name](operation=operation, payload=repeat_payload)
            if job is not None
            else tools[tool_name](operation=operation)
        )
        assert repeated["ok"] is True
        assert requests == [operation, operation]
        assert _snapshot(registry) != first_state
        with closing(sqlite3.connect(registry.path.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute("SELECT upgraded_at FROM manager_automation_schema").fetchone() == (
                "2026-01-03T00:00:00Z",
            )
            assert connection.execute("SELECT COUNT(*) FROM manager_automations").fetchone() == (int(job is not None),)
            if job is not None:
                assert connection.execute("SELECT desired_state,revision FROM manager_automations").fetchone() == (
                    "off",
                    job["revision"],
                )
            assert connection.execute("SELECT COUNT(*) FROM manager_automation_runs").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM manager_automation_outbox").fetchone() == (0,)
    return changed


@pytest.mark.parametrize("operation", ["status", "readiness"])
def test_g1_catalog_declares_actual_native_registry_metadata_writes(operation, g1_native_tools, isolated_http):
    changed = _exercise_g1(operation, g1_native_tools)
    assert isolated_http == []
    row = _catalog_row("manager_automations")
    assert row["execution_kind"] == "composed"
    _assert_effects_compatible(row, http_calls=[], state_changed=changed)


def test_g1_preview_metadata_writes_do_not_change_job_or_enqueue_outbox(g1_native_tools, isolated_http):
    changed = _exercise_g1("preview", g1_native_tools)
    assert isolated_http == []
    _assert_effects_compatible(_catalog_row("manager_automation_control"), http_calls=[], state_changed=changed)


@pytest.mark.parametrize("operation", ["status", "readiness", "preview"])
@pytest.mark.parametrize("false_kind", ["pure", "local_read", "network_read"])
def test_g1_effect_gate_rejects_false_read_metadata(operation, false_kind, g1_native_tools, isolated_http):
    changed = _exercise_g1(operation, g1_native_tools)
    assert isolated_http == []
    name = "manager_automation_control" if operation == "preview" else "manager_automations"
    row = deepcopy(_catalog_row(name))
    row["execution_kind"] = false_kind
    with pytest.raises(AssertionError, match="wrote Manager state"):
        _assert_effects_compatible(row, http_calls=[], state_changed=changed)


def test_g1_default_status_initializes_missing_synthetic_registry(g1_native_tools, isolated_http):
    tools, registry, _, _, requests, _ = g1_native_tools
    assert not registry.path.exists()
    assert tools["manager_automations"]()["ok"] is True
    assert registry.path.is_file() and requests == ["status"]
    assert isolated_http == []


@pytest.mark.parametrize("current_registry", [False, True])
def test_g1_templates_do_not_create_or_change_registry(current_registry, g1_native_tools, isolated_http):
    tools, registry, _, clock, requests, _ = g1_native_tools
    if current_registry:
        registry.initialize()
        before = _snapshot(registry)
    clock[0] = datetime(2026, 1, 2, tzinfo=UTC)
    assert tools["manager_automations"](operation="templates")["ok"] is True
    assert requests == ["templates"] and isolated_http == []
    if current_registry:
        assert _snapshot(registry) == before
    else:
        assert not registry.path.exists()


def test_g1_create_template_preview_does_not_create_registry(g1_native_tools, isolated_http):
    tools, registry, _, _, requests, _ = g1_native_tools
    assert (
        tools["manager_automation_control"](
            operation="preview",
            payload={"target_operation": "create_from_template", "target_payload": {"template_id": "crm_digest_v1"}},
        )["ok"]
        is True
    )
    assert requests == ["preview"] and isolated_http == []
    assert not registry.path.exists()


J1_READERS = ("j1_research_status", "j1_research_results", "j1_research_report")
J1_VIN_FACADES = (
    "j1_research_vin",
    "j1_research_record_facts",
    *J1_READERS,
    "j1_research_document",
    "j1_research_add_queries",
    "j1_research_cancel",
)


@pytest.fixture
def j1_native_tools(tmp_path, monkeypatch, isolated_http):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
    clock = [datetime(2030, 1, 1, tzinfo=UTC).timestamp()]
    monkeypatch.setattr(j1_vin_store.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        j1_research, "_utcnow", lambda: datetime.fromtimestamp(clock[0], UTC).isoformat(timespec="seconds")
    )
    with tempfile.TemporaryDirectory(prefix="catalog-j1-tmpfs-", dir="/dev/shm") as private_root:
        root = Path(private_root)
        assert j1_vin_store._is_tmpfs(root) and root.stat().st_mode & 0o777 == 0o700
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", private_root)
        server = FastMCP("j1-catalog-effects-test")
        mcp_tools.register_manager_tools(
            server, StoreState(tmp_path / "unused-manager.sqlite3"), include_tools=J1_VIN_FACADES
        )
        yield server._tool_manager._tools, root, clock
    assert not root.exists()


def _j1_arguments(name, job_id):
    if name == "j1_research_vin":
        return {"vin": SYNTHETIC_VIN, "idempotency_key": "synthetic-j1-start"}
    if name == "j1_research_record_facts":
        return {"job_id": job_id, "expected_revision": 0, "facts": [], "idempotency_key": "synthetic-j1-facts"}
    if name == "j1_research_document":
        return {"job_id": job_id, "document_id": "d" * 32}
    if name == "j1_research_add_queries":
        return {"job_id": job_id, "queries": ["synthetic public technical references"]}
    return {"job_id": job_id}


def _j1_stub(job_id):
    with j1_research._db(readonly=True) as connection:
        return dict(connection.execute("SELECT status,error,updated_at FROM jobs WHERE id=?", (job_id,)).fetchone())


@pytest.mark.parametrize("name", J1_READERS)
def test_j1_reader_native_missing_job_initializes_storage_and_cannot_advertise_read_only(
    name, j1_native_tools, isolated_http
):
    tools, _root, _clock = j1_native_tools
    database = j1_research._db_path()
    assert not database.exists()
    result = tools[name].fn(job_id="0" * 32)
    assert not result["ok"] and result["error"]["code"] == "job_not_found"
    assert database.is_file() and database.stat().st_size > 0
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "queries", "documents", "documents_fts"}.issubset(tables)
    _assert_effects_compatible(_catalog_row(name), http_calls=[], state_changed=True)
    assert tools[name].annotations.readOnlyHint is False
    assert isolated_http == []


@pytest.mark.parametrize("name", J1_VIN_FACADES)
@pytest.mark.parametrize("scenario", ["expiry", "failed_worker_reconciliation"])
def test_j1_native_lifecycle_effects_are_covered_by_whole_tool_hints(name, scenario, j1_native_tools, isolated_http):
    tools, root, clock = j1_native_tools
    start = tools["j1_research_vin"].fn(**_j1_arguments("j1_research_vin", ""))
    assert start["ok"]
    job_id = start["job_id"]
    private_database = root / job_id / "research.sqlite3"
    if scenario == "failed_worker_reconciliation":
        with j1_vin_store.connect(job_id, transaction=True) as connection:
            current = j1_vin_store.metadata(connection)
            current.update(collection_status="running", inflight=True, operation_started_at=clock[0] - 20)
            j1_vin_store.write_metadata(connection, current)
            connection.execute("UPDATE queries SET status='running'")
        j1_vin_store.set_stub(job_id, "failed", "synthetic_worker_failed")
    before_stub = _j1_stub(job_id)
    before_private = private_database.read_bytes()
    if scenario == "expiry":
        clock[0] += j1_vin_store.RETENTION_SECONDS + 2
    arguments = _j1_arguments(name, job_id)
    result = tools[name].fn(**arguments)
    if scenario == "expiry":
        assert not (root / job_id).exists(), "actual facade must delete this expired temporary corpus"
        first_stub = _j1_stub(job_id)
        assert first_stub != before_stub and first_stub["status"] == "failed"
        assert first_stub["error"] in {"vin_job_expired", "vin_ephemeral_state_lost"}
        if name == "j1_research_vin":
            assert result["ok"] and result["job_id"] != job_id
        else:
            assert not result["ok"] and result["error"]["code"] == "vin_ephemeral_state_lost"
            clock[0] += 2
            assert not tools[name].fn(**arguments)["ok"]
            assert _j1_stub(job_id) != first_stub, "identical repeat must demonstrate the extra timestamp write"
    else:
        assert private_database.read_bytes() != before_private
        with j1_vin_store.connect(job_id) as connection:
            current = j1_vin_store.metadata(connection)
            assert current["collection_status"] in {"failed", "cancelled"}
            assert not current["inflight"] and current["used_seconds"] == 20
            assert connection.execute("SELECT COUNT(*) FROM queries WHERE status='running'").fetchone()[0] == 0
    hints = tools[name].annotations
    assert hints.readOnlyHint is False
    assert hints.destructiveHint is True, "whole-tool hint must cover the expiry case, including nominal readers"
    assert hints.idempotentHint is False, "domain deduplication does not cover lifecycle housekeeping"
    _assert_effects_compatible(_catalog_row(name), http_calls=[], state_changed=True)
    assert isolated_http == []


@pytest.mark.parametrize("name", ["j1_research_vin", "j1_research_record_facts"])
def test_j1_disabled_native_writes_still_clean_expired_private_state(name, j1_native_tools, monkeypatch, isolated_http):
    tools, root, clock = j1_native_tools
    job_id = tools["j1_research_vin"].fn(**_j1_arguments("j1_research_vin", ""))["job_id"]
    clock[0] += j1_vin_store.RETENTION_SECONDS + 2
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    result = tools[name].fn(**_j1_arguments(name, job_id))
    expected_error = "vin_research_disabled" if name == "j1_research_vin" else "vin_ephemeral_state_lost"
    assert not result["ok"] and result["error"]["code"] == expected_error
    assert not (root / job_id).exists()
    assert _j1_stub(job_id)["status"] == "failed"
    assert isolated_http == []


@pytest.mark.parametrize("name", ["j1_research_vin", "j1_research_record_facts"])
def test_j1_scoped_receipts_still_deduplicate_live_domain_operations(name, j1_native_tools, isolated_http):
    tools, _root, _clock = j1_native_tools
    job_id = tools["j1_research_vin"].fn(**_j1_arguments("j1_research_vin", ""))["job_id"]
    arguments = _j1_arguments(name, job_id)
    first = tools[name].fn(**arguments)
    assert first["ok"]
    with j1_vin_store.connect(job_id) as connection:
        before = tuple(connection.iterdump())
    stub_before = _j1_stub(job_id)
    second = tools[name].fn(**arguments)
    assert {k: v for k, v in first.items() if k != "tool_execution"} == {
        k: v for k, v in second.items() if k != "tool_execution"
    }
    with j1_vin_store.connect(job_id) as connection:
        assert tuple(connection.iterdump()) == before
    assert _j1_stub(job_id) == stub_before
    assert isolated_http == []
