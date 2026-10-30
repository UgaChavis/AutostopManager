"""Read-only partial process observations; this is not a service drain."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


class MCPObservation:
    """One server's HTTP scope counter, never a drain proof."""

    def __init__(self) -> None:
        self._initialize()

    def _initialize(self) -> None:
        self._pid = os.getpid()
        self.instance_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._active: set[object] = set()

    def bind_process(self) -> None:
        """After a pre-fork server setup, give each child its own state."""
        if self._pid != os.getpid():
            self._initialize()

    def acquire(self) -> object:
        with self._lock:
            token = object()
            self._active.add(token)
            return token

    def release(self, token: object) -> None:
        with self._lock:
            self._active.remove(token)

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = len(self._active)
        return {
            "format": "manager_mcp_observation_v1",
            "instance_id": self.instance_id,
            "pid": self._pid,
            "http_scopes_active": active,
            "admission_control": "unavailable",
            # HTTP scopes can end before MCP sessions, workers or their
            # children. Neither HTTP nor the observed pools is a full census.
            "service_drained": None,
            "safe_to_stop": False,
            "untracked_work": "unknown",
        }


class MCPObservationMiddleware:
    def __init__(self, app: ASGIApp, observation: MCPObservation) -> None:
        self.app = app
        self.observation = observation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = self.observation.acquire()
        try:
            await self.app(scope, receive, send)
        finally:
            self.observation.release(token)


def worker_observations(store_workers: Any) -> dict[str, Any]:
    """Read known counters individually; all are partial, non-atomic samples."""
    # Do not import modules or create pools just to observe them. An unloaded
    # module, missing pool or failed property is unknown, never a false zero.
    process_pools = {
        "avito": ("listing_executor", "_EXECUTOR"),
        "automotive_batch": ("automotive_identity", "_BATCH_READ_EXECUTOR"),
        "read": ("mcp_read_executor", "_EXECUTOR"),
        "web": ("mcp_read_executor", "_WEB_EXECUTOR"),
        "control": ("mcp_read_executor", "_CONTROL_EXECUTOR"),
        "j1_status": ("mcp_read_executor", "_J1_STATUS_EXECUTOR"),
        "j1_report": ("mcp_read_executor", "_J1_REPORT_EXECUTOR"),
        "j1_results": ("mcp_read_executor", "_J1_RESULTS_EXECUTOR"),
        "j1_document": ("mcp_read_executor", "_J1_DOCUMENT_EXECUTOR"),
        "j1_add_queries": ("mcp_read_executor", "_J1_ADD_QUERIES_EXECUTOR"),
        "j1_cancel": ("mcp_read_executor", "_J1_CANCEL_EXECUTOR"),
        "j1_start": ("mcp_read_executor", "_J1_START_EXECUTOR"),
        "j1_record_facts": ("mcp_read_executor", "_J1_RECORD_FACTS_EXECUTOR"),
        "j1_vin_start": ("mcp_read_executor", "_J1_VIN_START_EXECUTOR"),
        "drom_start": ("mcp_drom_executor", "_START_EXECUTOR"),
        "drom_get": ("mcp_drom_executor", "_GET_EXECUTOR"),
        "store_fallback_read": ("mcp_store_executor", "_READ_EXECUTOR"),
        "store_fallback_stateful": ("mcp_store_executor", "_STATEFUL_EXECUTOR"),
    }
    instance_pools = {
        "store_read": store_workers.reads,
        "store_stateful": store_workers.stateful,
        "store_transport": store_workers.transport,
    }

    def sample(pool: Any) -> int | None:
        try:
            value = pool.pending_count
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        except Exception:  # noqa: BLE001 - an unavailable observation must not become a false zero.
            return None

    process = {}
    for name, (module_name, attribute) in process_pools.items():
        module = sys.modules.get(f"{__package__}.{module_name}")
        process[name] = sample(getattr(module, attribute, None))

    return {
        "complete": False,
        "atomic": False,
        "instance_pending": {name: sample(pool) for name, pool in instance_pools.items()},
        "shared_process_pending": process,
    }
