"""J1 public routing errors: real readonly SQLite failure, no general fallback.

Twenty-five distinct cases plus two native add_queries repeats (27 pytest runs).
The native repeats exercise the SDK's JSON-string-to-list conversion only.
Linux private-store fixtures use disposable real tmpfs, as the VIN store requires.
No worker, network, process, live database or production credential is used.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, j1_research as j1, j1_vin_research as vin, j1_vin_store
from autostop_manager import mcp_telemetry, mcp_tools
from autostop_manager.storage import StoreState


OPERATIONS = ("status", "results", "document", "report", "add_queries", "cancel")
VIN = "Z94K241BBKR000000"  # Constructed test value, not a vehicle assertion.
DOCUMENT_ID = "d" * 32
UNKNOWN_ID = "f" * 32
FOLLOWUP = "Synthetic public followup evidence"


def _forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected fallback, transport, process or worker boundary")


@pytest.fixture(autouse=True)
def isolated_stores(monkeypatch, tmp_path):
    with tempfile.TemporaryDirectory(prefix="j1-routing-test-", dir="/dev/shm") as temporary:
        private = Path(temporary)
        assert j1_vin_store._is_tmpfs(private)
        assert private.stat().st_mode & 0o777 == 0o700
        monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
        monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(private))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
        monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "")
        monkeypatch.setattr(config, "_ENV_LOADED", False)
        monkeypatch.setattr(j1, "search_public", _forbidden)
        monkeypatch.setattr(j1, "fetch_document", _forbidden)
        for name in ("connect", "connect_ex"):
            monkeypatch.setattr(socket.socket, name, _forbidden)
        for name in ("create_connection", "getaddrinfo"):
            monkeypatch.setattr(socket, name, _forbidden)
        # Patch after SDK imports: Popen remains a class during type evaluation.
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "run", _forbidden)
        yield private


def _job(profile):
    if profile == "vin":
        created = vin.j1_research_vin(VIN, "synthetic-routing-start")
        assert created["ok"] is True, created
        return created["job_id"]
    created = j1.start_research("Synthetic public evidence", ["Synthetic initial query"])
    assert created["ok"] is True, created
    if profile == "unknown":
        return UNKNOWN_ID
    with j1._db() as connection:
        connection.execute(
            "INSERT INTO documents(id,job_id,url,title,body,status,kind,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                DOCUMENT_ID,
                created["job_id"],
                "https://example.org/synthetic",
                "Synthetic public source",
                "Synthetic public technical evidence without personal data.",
                "fetched",
                "html",
                j1._utcnow(),
            ),
        )
        connection.commit()
    return created["job_id"]


def _arguments(operation, job_id):
    arguments = {"job_id": job_id}
    if operation == "document":
        arguments["document_id"] = DOCUMENT_ID
    if operation == "add_queries":
        arguments["queries"] = [FOLLOWUP]
    return arguments


def _call(operation, job_id):
    arguments = _arguments(operation, job_id)
    snapshot = deepcopy(arguments)
    result = getattr(j1, f"research_{operation}")(**arguments)
    assert arguments == snapshot
    return result


def _snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def corrupt_route(monkeypatch, tmp_path, isolated_stores):
    job_id = _job("vin")
    healthy_root = j1._db_path().parent
    broken_root = tmp_path / "broken"
    broken_root.mkdir()
    broken_path = broken_root / j1._db_path().name
    broken_path.write_bytes(b"Synthetic corrupt disposable routing database.\n" * 16)
    roots = (healthy_root, isolated_stores, broken_root)
    before = [_snapshot(root) for root in roots]
    opens = []
    original_db = j1._db

    @contextmanager
    def observed_db(*, readonly=False):
        opens.append(readonly)
        assert readonly, "unknown routing must not reach the ordinary writable DB"
        with original_db(readonly=readonly) as connection:
            yield connection

    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(broken_root))
    monkeypatch.setattr(j1, "_db", observed_db)
    for operation in OPERATIONS:
        monkeypatch.setattr(vin, f"research_{operation}", _forbidden)
    try:
        yield job_id
    finally:
        assert opens and all(opens)
        assert [_snapshot(root) for root in roots] == before


def _assert_routing_error(result, operation, job_id):
    assert result == {
        "ok": False,
        "schema": j1.REPORT_SCHEMA if operation == "report" else j1.SCHEMA,
        "job_id": job_id,
        "error": {"code": "vin_store_unavailable"},
    }


@pytest.mark.parametrize("operation", OPERATIONS)
def test_public_facades_return_routing_error_without_fallback(corrupt_route, operation):
    _assert_routing_error(_call(operation, corrupt_route), operation, corrupt_route)


def test_internal_worker_keeps_routing_failure_as_exception(corrupt_route):
    with pytest.raises(j1_vin_store.VinJobError) as caught:
        j1._work_job(corrupt_route)
    assert caught.value.code == "vin_store_unavailable"


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("profile", ("ordinary", "vin", "unknown"))
def test_healthy_routes_keep_existing_contracts(profile, operation):
    job_id = _job(profile)
    result = _call(operation, job_id)
    if profile == "vin":
        schema = j1_vin_store.REPORT_SCHEMA if operation == "report" else j1_vin_store.SCHEMA
    else:
        schema = j1.REPORT_SCHEMA if operation == "report" else j1.SCHEMA
    assert result["schema"] == schema
    assert result["job_id"] == job_id
    if profile == "unknown" or (profile == "vin" and operation == "document"):
        assert result["ok"] is False
        expected = "document_not_found" if operation == "document" else "job_not_found"
        assert result["error"]["code"] == expected
    else:
        assert result["ok"] is True
        if operation == "add_queries":
            assert result["added"] == 1


@pytest.fixture
def native_add_queries(monkeypatch, tmp_path):
    class InertAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, InertAdapter)
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)
    server = FastMCP("isolated-j1-routing")
    name = "j1_research_add_queries"
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "native.sqlite3"), include_tools={name})
    assert set(server._tool_manager._tools) == {name}

    def call(job_id):
        arguments = {"job_id": job_id, "queries": json.dumps([FOLLOWUP])}
        tool = server._tool_manager.get_tool(name)
        content, result = asyncio.run(tool.run(arguments, convert_result=True))
        assert json.loads(content[0].text) == result
        return result

    return call


def test_native_json_queries_preserve_healthy_route(native_add_queries):
    job_id = _job("ordinary")
    result = native_add_queries(job_id)
    assert result["ok"] is True
    assert result["schema"] == j1.SCHEMA
    assert result["job_id"] == job_id
    assert result["added"] == 1
    with j1._db(readonly=True) as connection:
        queries = [row[0] for row in connection.execute("SELECT query FROM queries WHERE job_id=?", (job_id,))]
    assert FOLLOWUP in queries
    assert len(queries) == 2


def test_native_json_queries_preserve_routing_error(corrupt_route, native_add_queries):
    _assert_routing_error(native_add_queries(corrupt_route), "add_queries", corrupt_route)
