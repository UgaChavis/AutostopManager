"""Bound a synchronous HTTP read across headers and body, not each socket read.

The worker owns an async stream with a monotonic deadline. Cancelling a caller's
to_thread await cannot stop its Python thread; this transport's own deadline
still cancels and closes the HTTP stream. OS DNS resolution is not forcibly
terminated, and a bounded caller wait does not claim worker termination.
"""

from __future__ import annotations

import asyncio
import io
import math
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from email.message import Message
from http.client import HTTPException
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import anyio
import httpx


ERROR_BODY_LIMIT = 4096
MAX_TRANSPORT_WORKERS = 2
# Retained until asyncio.run also finishes DNS/default-executor shutdown.
_ADMISSIONS = threading.BoundedSemaphore(MAX_TRANSPORT_WORKERS)


class TrackedTransportAdmission:
    """Nonblocking capacity retained through the transport worker's real cleanup."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("invalid_transport_capacity")
        self._capacity = capacity
        self._pending = 0
        self._accepting = True
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = False) -> bool:
        if blocking:
            raise ValueError("transport_admission_must_not_block")
        with self._lock:
            if not self._accepting or self._pending >= self._capacity:
                return False
            self._pending += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._pending == 0:
                raise ValueError("transport_admission_released_too_many_times")
            self._pending -= 1

    def begin_drain(self) -> None:
        with self._lock:
            self._accepting = False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending


class BufferedResponse(io.BytesIO):
    def __init__(self, body: bytes, *, status: int, headers: Message, url: str) -> None:
        super().__init__(body)
        self.status, self.code, self.headers, self.url = status, status, headers, url

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def info(self) -> Message:
        return self.headers


def _response_headers(response: httpx.Response) -> Message:
    headers = Message()
    for key, value in response.headers.multi_items():
        headers[key] = value
    return headers


@asynccontextmanager
async def _stream_response(
    client: httpx.AsyncClient, request: httpx.Request, *, follow_redirects: bool
) -> AsyncIterator[httpx.Response]:
    response = None
    try:
        for redirects in range(client.max_redirects + 1):
            # Resolve initial URL/client auth once, as HTTPX's automatic chain
            # does. Later requests already carry its redirect header policy;
            # deriving BasicAuth again from Location userinfo would replace it.
            response = await client.send(
                request,
                stream=True,
                follow_redirects=False,
                auth=httpx.USE_CLIENT_DEFAULT if redirects == 0 else httpx.Auth(),
            )
            if not follow_redirects or response.next_request is None:
                yield response
                return
            # HTTPX constructs the next URL/method/auth/cookie request. Closing
            # here avoids its automatic unbounded read of intermediate bodies.
            request = response.next_request
            await response.aclose()
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
    finally:
        if response is not None:
            await response.aclose()


async def _fetch(
    request: Request,
    *,
    deadline: float,
    maximum: int,
    follow_redirects: bool,
    truncate: bool = False,
    error_maximum: int = ERROR_BODY_LIMIT,
    on_error_headers: Callable[[int, Message], None] | None = None,
    response_maximum: Callable[[Message], int] | None = None,
) -> BufferedResponse:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("provider_total_deadline_exceeded")

    async def remember_rejection(response: httpx.Response) -> None:
        if on_error_headers is not None and not 200 <= response.status_code < 300:
            headers = _response_headers(response)
            on_error_headers(response.status_code, headers)
            if 300 <= response.status_code < 400:
                raise HTTPError(
                    request.full_url, response.status_code, f"HTTP status {response.status_code}", headers, io.BytesIO()
                )

    try:
        # Keep proxy/certificate environment handling and TLS verification enabled.
        with anyio.fail_after(remaining):
            async with httpx.AsyncClient(
                timeout=remaining,
                trust_env=True,
                follow_redirects=follow_redirects,
                max_redirects=10,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                if on_error_headers is not None and not follow_redirects:
                    # HTTPX parses Location even when it will not follow it.
                    # Reject redirects before their parsing can lose dispatch/status.
                    client.event_hooks["response"].append(remember_rejection)
                # Client/TLS initialization can run synchronously before an async checkpoint.
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider_total_deadline_exceeded")
                http_request = client.build_request(
                    request.get_method(),
                    request.full_url,
                    content=cast(Any, request.data),
                    headers=dict(request.header_items()),
                )
                async with _stream_response(client, http_request, follow_redirects=follow_redirects) as response:
                    headers = _response_headers(response)
                    failed = not 200 <= response.status_code < 300
                    if failed and on_error_headers is not None and follow_redirects:
                        on_error_headers(response.status_code, headers)
                    limit = error_maximum if failed else maximum
                    if not failed and response_maximum is not None:
                        selected = response_maximum(headers)
                        if isinstance(selected, bool) or not isinstance(selected, int) or selected <= 0:
                            raise ValueError("invalid_response_size_limit")
                        limit = min(limit, selected)
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        remaining_bytes = limit + 1 - len(body)
                        body.extend(chunk[:remaining_bytes])
                        if truncate and not failed and len(body) >= limit:
                            del body[limit:]
                            break  # Prefix readers must not wait for an extra byte or EOF.
                        if len(body) > limit:
                            if failed:
                                break  # A bounded error body is enough for quota/auth classification.
                            raise ValueError("provider_response_too_large")
                    if failed:
                        raise HTTPError(
                            request.full_url,
                            response.status_code,
                            f"HTTP status {response.status_code}",
                            headers,
                            io.BytesIO(bytes(body)),
                        )
                    return BufferedResponse(
                        bytes(body), status=response.status_code, headers=headers, url=str(response.url)
                    )
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise TimeoutError("provider_total_deadline_exceeded") from exc
    except httpx.HTTPError as exc:
        raise URLError("provider_network_error") from exc


def urlopen(
    request: Request,
    *,
    timeout: float,
    maximum: int,
    follow_redirects: bool = False,
    truncate: bool = False,
    error_maximum: int = ERROR_BODY_LIMIT,
    admission: threading.BoundedSemaphore | TrackedTransportAdmission | None = None,
    deadline: float | None = None,
    preserve_error_status: bool = False,
    response_maximum: Callable[[Message], int] | None = None,
) -> BufferedResponse:
    """Urllib-compatible buffered response; its read() never waits on a socket.

    Run in an owned thread so the same synchronous entry point also works when
    called by a running event loop. Existing facade urlopen monkeypatchs stay at
    their current boundary and do not need to imitate httpx.
    Prefix readers opt into truncate; the default still rejects oversized bodies.
    A supplied monotonic deadline can only shorten the relative timeout. Callers
    may isolate transport capacity and retain a known HTTP error status when its
    body cannot be completed; the original provider defaults remain unchanged.
    A response header selector may lower, never increase, the success body limit.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_http_deadline")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("invalid_response_size_limit")
    if isinstance(error_maximum, bool) or not isinstance(error_maximum, int) or error_maximum <= 0:
        raise ValueError("invalid_error_response_size_limit")
    if response_maximum is not None and not callable(response_maximum):
        raise ValueError("invalid_response_size_selector")
    if deadline is not None and (
        isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
    ):
        raise ValueError("invalid_http_deadline")
    started_at = time.monotonic()
    deadline = min(started_at + timeout, deadline) if deadline is not None else started_at + timeout
    if deadline <= started_at:
        raise TimeoutError("provider_total_deadline_exceeded")
    admission = _ADMISSIONS if admission is None else admission
    if not admission.acquire(blocking=False):
        raise URLError("provider_transport_busy")
    try:
        completed = threading.Event()
        outcome: list[tuple[bool, Any]] = []
        error_lock = threading.Lock()
        error_response: tuple[int, Message] | None = None

        def remember_error_headers(status: int, headers: Message) -> None:
            nonlocal error_response
            with error_lock:
                error_response = status, headers

        def incomplete_http_error() -> HTTPError | None:
            with error_lock:
                observed = error_response
            if observed is None:
                return None
            status, headers = observed
            return HTTPError(request.full_url, status, f"HTTP status {status}", headers, io.BytesIO())

        def worker() -> None:
            try:
                response = asyncio.run(
                    _fetch(
                        request,
                        deadline=deadline,
                        maximum=maximum,
                        follow_redirects=follow_redirects,
                        truncate=truncate,
                        error_maximum=error_maximum,
                        on_error_headers=remember_error_headers if preserve_error_status else None,
                        response_maximum=response_maximum,
                    )
                )
                outcome.append((True, response))
            except BaseException as exc:  # noqa: BLE001 - hand the original exception back to the owning caller.
                outcome.append((False, exc))
            finally:
                admission.release()
                completed.set()

        try:
            worker_thread = threading.Thread(target=worker, name="autostop-bounded-http", daemon=True)
        except RuntimeError:
            raise URLError("provider_transport_unavailable") from None
    except BaseException:
        # Setup has not started a worker, so ownership is still with this caller.
        admission.release()
        raise

    try:
        worker_thread.start()
    except RuntimeError:
        admission.release()
        raise URLError("provider_transport_unavailable") from None
    if not completed.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        if preserve_error_status and (error := incomplete_http_error()) is not None:
            raise error
        raise TimeoutError("provider_total_deadline_exceeded")
    succeeded, value = outcome[0]
    if not succeeded:
        if (
            preserve_error_status
            and not isinstance(value, HTTPError)
            and isinstance(value, (OSError, HTTPException))
            and (error := incomplete_http_error()) is not None
        ):
            raise error from value
        raise value
    return value
