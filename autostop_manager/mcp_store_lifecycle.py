"""Local pre-stop handshake and application-level Store worker shutdown."""

from __future__ import annotations

from contextlib import asynccontextmanager
import ipaddress
import logging
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .mcp_store_executor import StoreWorkers


LOGGER = logging.getLogger(__name__)
SHUTDOWN_DRAIN_SECONDS = 10.0
INSTANCE_HEADER = "X-Autostop-Store-Instance"


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_control_request(request: Request) -> bool:
    # Uvicorn can rewrite scope.client from forwarded headers. Reject those
    # headers before inspecting the peer; custom FastMCP routes have no MCP
    # transport's Host/Origin middleware of their own.
    if any(key == "forwarded" or key.startswith("x-forwarded-") for key in request.headers):
        return False
    if "origin" in request.headers or request.client is None or not _loopback(request.client.host):
        return False
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        return False
    server = request.scope.get("server")
    if server is None:
        return False
    # Compare the original header rather than parsing/normalizing an attacker
    # supplied URL (which can silently discard controls, userinfo or empty parts).
    names = {"127.0.0.1", "[::1]", "localhost"}
    if _loopback(server[0]):
        names.add(f"[{server[0]}]" if ":" in server[0] else server[0])
    allowed = {f"{host}:{server[1]}" for host in names}
    if server[1] == 80:
        allowed.update(names)
    return hosts[0] in allowed


def _response(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


class StoreFastMCP(FastMCP):
    def __init__(self, *args: Any, store_workers: StoreWorkers | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.store_workers = store_workers or StoreWorkers()
        self.custom_route("/internal/store-workers", methods=["GET"], include_in_schema=False)(self._worker_status)
        self.custom_route("/internal/store-workers/drain", methods=["POST"], include_in_schema=False)(
            self._begin_worker_drain
        )

    async def _worker_status(self, request: Request) -> JSONResponse:
        if not _local_control_request(request):
            return _response({"error_code": "store_control_local_request_required"}, 403)
        return _response(self.store_workers.status())

    async def _begin_worker_drain(self, request: Request) -> JSONResponse:
        if not _local_control_request(request):
            return _response({"error_code": "store_control_local_request_required"}, 403)
        if request.headers.getlist(INSTANCE_HEADER) != [self.store_workers.instance_id]:
            return _response({"error_code": "store_control_instance_mismatch"}, 409)
        if request.headers.getlist("content-length") not in ([], ["0"]) or "transfer-encoding" in request.headers:
            return _response({"error_code": "store_control_empty_body_required"}, 400)
        snapshot = self.store_workers.begin_drain()
        return _response(snapshot, 200 if snapshot["drained"] else 202)

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: Starlette):
            # FastMCP's own lifespan is per request in stateless mode. Keep its
            # session manager unchanged and attach shutdown to the outer app.
            async with original_lifespan(application) as state:
                try:
                    yield state
                finally:
                    # A caller/AnyIO cancellation must not cancel an accepted
                    # Store write. This is only a fallback AFTER Uvicorn's HTTP
                    # drain; the controller must do the handshake before SIGTERM.
                    with anyio.CancelScope(shield=True):
                        report = await self.store_workers.drain(SHUTDOWN_DRAIN_SECONDS)
                        if report["ok"]:
                            self.store_workers.close()
                        else:
                            LOGGER.error("store_drain_timeout pending=%s", report["pending"])

        app.router.lifespan_context = lifespan
        return app
