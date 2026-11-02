"""Native shutdown must retain accepted J1 work after its caller goes away."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

import pytest

from autostop_manager import mcp_read_executor, mcp_server
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_store_lifecycle import StoreFastMCP
from autostop_manager.mcp_telemetry import instrument_manager_tools


POOL_NAMES = (
    "_J1_STATUS_EXECUTOR",
    "_J1_REPORT_EXECUTOR",
    "_J1_RESULTS_EXECUTOR",
    "_J1_DOCUMENT_EXECUTOR",
    "_J1_ADD_QUERIES_EXECUTOR",
    "_J1_CANCEL_EXECUTOR",
    "_J1_START_EXECUTOR",
    "_J1_RECORD_FACTS_EXECUTOR",
    "_J1_VIN_START_EXECUTOR",
)
TOOL_NAMES = (
    "j1_research_status",
    "j1_research_report",
    "j1_research_results",
    "j1_research_document",
    "j1_research_add_queries",
    "j1_research_cancel",
    "j1_research_start",
    "j1_research_record_facts",
    "j1_research_vin",
)


@pytest.mark.parametrize("active_index", range(len(POOL_NAMES)), ids=POOL_NAMES)
def test_shutdown_waits_for_cancelled_callers_real_future(monkeypatch, active_index):
    pools = [BoundedListingExecutor(max_workers=1, max_waiting_calls=1) for _ in POOL_NAMES]
    for name, pool in zip(POOL_NAMES, pools, strict=True):
        monkeypatch.setattr(mcp_read_executor, name, pool)
    server = StoreFastMCP("synthetic-j1-shutdown", stateless_http=True)
    server.drain_j1_on_shutdown = True
    app = server.streamable_http_app()
    release = threading.Event()

    async def exercise():
        started = asyncio.Event()
        scope_exiting = asyncio.Event()
        loop = asyncio.get_running_loop()

        def work(objective: str):
            assert objective == "synthetic"
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            return {"ok": True}

        server.tool(name=TOOL_NAMES[active_index])(work)
        instrument_manager_tools(server)
        tool = server._tool_manager.get_tool(TOOL_NAMES[active_index])

        async def lifetime():
            async with app.router.lifespan_context(app):
                call = asyncio.create_task(tool.run({"objective": "synthetic"}))
                await asyncio.wait_for(started.wait(), 2)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                assert pools[active_index].pending_count == 1
                scope_exiting.set()

        shutdown = asyncio.create_task(lifetime())
        try:
            await asyncio.wait_for(scope_exiting.wait(), 2)
            for _ in range(100):
                if all(pool._draining for pool in pools):
                    break
                await asyncio.sleep(0.01)
            assert all(pool._draining for pool in pools)
            assert not shutdown.done()
            for pool in pools:
                with pytest.raises(ListingExecutionError, match="provider_executor_draining"):
                    await pool.run(lambda: {"ok": True})
            release.set()
            await asyncio.wait_for(shutdown, 2)
            assert all(pool.pending_count == 0 for pool in pools)
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await shutdown

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        for pool in pools:
            pool.close()


def test_native_entrypoint_enables_process_wide_shutdown(monkeypatch):
    class Server:
        drain_j1_on_shutdown = False

        def run(self, *, transport):
            assert self.drain_j1_on_shutdown is True
            assert transport == "streamable-http"

    monkeypatch.setattr(mcp_server, "build_server", Server)
    mcp_server.main()


@pytest.mark.parametrize("active_index", range(len(POOL_NAMES)), ids=POOL_NAMES)
def test_store_shutdown_error_does_not_abandon_j1_future(monkeypatch, active_index):
    pools = [BoundedListingExecutor(max_workers=1, max_waiting_calls=1) for _ in POOL_NAMES]
    for name, item in zip(POOL_NAMES, pools, strict=True):
        monkeypatch.setattr(mcp_read_executor, name, item)
    pool = pools[active_index]
    server = StoreFastMCP("synthetic-j1-store-error", stateless_http=True)
    server.drain_j1_on_shutdown = True
    app = server.streamable_http_app()
    release = threading.Event()

    async def fail_store(_timeout):
        raise RuntimeError("synthetic_store_shutdown")

    monkeypatch.setattr(server.store_workers, "drain", fail_store)

    async def exercise():
        started = asyncio.Event()
        exiting = asyncio.Event()
        loop = asyncio.get_running_loop()

        def work():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            return {"ok": True}

        async def lifetime():
            async with app.router.lifespan_context(app):
                call = asyncio.create_task(pool.run(work))
                await asyncio.wait_for(started.wait(), 2)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                exiting.set()

        shutdown = asyncio.create_task(lifetime())
        try:
            await asyncio.wait_for(exiting.wait(), 2)
            await asyncio.sleep(0.03)
            assert not shutdown.done() and pool.pending_count == 1
            assert all(item._draining for item in pools)
            release.set()
            with pytest.raises(ExceptionGroup) as raised:
                await asyncio.wait_for(shutdown, 2)
            assert (
                raised.value.subgroup(
                    lambda error: isinstance(error, RuntimeError) and str(error) == "synthetic_store_shutdown"
                )
                is not None
            )
            assert all(item.pending_count == 0 for item in pools)
        finally:
            release.set()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        for item in pools:
            item.close()
        server.store_workers.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics required")
def test_uvicorn_sigterm_waits_for_cancelled_j1_future(tmp_path):
    script = r"""
import asyncio, pathlib, sys, threading
from contextlib import asynccontextmanager
import uvicorn
from autostop_manager.mcp_read_executor import _J1_START_EXECUTOR
from autostop_manager.mcp_store_lifecycle import StoreFastMCP

root = pathlib.Path(sys.argv[1])
server = StoreFastMCP("synthetic-j1-process", stateless_http=True)
server.drain_j1_on_shutdown = True
app = server.streamable_http_app()
started = threading.Event()

def work():
    started.set()
    while not (root / "release").exists():
        threading.Event().wait(0.01)
    (root / "completed").write_text("done")
    return {"ok": True}

original_lifespan = app.router.lifespan_context
@asynccontextmanager
async def synthetic_lifespan(application):
    async with original_lifespan(application) as state:
        caller = asyncio.create_task(_J1_START_EXECUTOR.run(work))
        while not started.is_set():
            await asyncio.sleep(0.01)
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        print("READY", flush=True)
        yield state
app.router.lifespan_context = synthetic_lifespan

uvicorn.run(app, host="127.0.0.1", port=0, log_level="warning", access_log=False)
"""
    env = os.environ.copy()
    env.update(
        AUTOSTOP_MANAGER_ENV_FILE=os.devnull,
        AUTOSTOP_MANAGER_DB=str(tmp_path / "manager.sqlite3"),
        AUTOSTOP_J1_CACHE_DIR=str(tmp_path / "j1"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], "synthetic subprocess start timed out"
        line = process.stdout.readline().strip()
        if line != "READY":
            _, stderr = process.communicate(timeout=5)
            pytest.fail(f"synthetic subprocess did not start: {stderr}")
        process.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        assert process.poll() is None
        assert not (tmp_path / "completed").exists()
        (tmp_path / "release").touch()
        _, stderr = process.communicate(timeout=5)
        assert (tmp_path / "completed").read_text() == "done"
        assert "j1_shutdown_drained" in stderr
        assert process.returncode in (0, -signal.SIGTERM)
    finally:
        if process.poll() is None:
            (tmp_path / "release").touch()
            process.kill()
            process.communicate(timeout=5)
