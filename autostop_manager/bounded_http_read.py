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
from email.message import Message
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import anyio
import httpx


ERROR_BODY_LIMIT = 4096
MAX_TRANSPORT_WORKERS = 2
# Retained until asyncio.run also finishes DNS/default-executor shutdown.
_ADMISSIONS = threading.BoundedSemaphore(MAX_TRANSPORT_WORKERS)


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


async def _fetch(request: Request, *, deadline: float, maximum: int, follow_redirects: bool) -> BufferedResponse:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("provider_total_deadline_exceeded")
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
                # Client/TLS initialization can run synchronously before an async checkpoint.
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider_total_deadline_exceeded")
                async with client.stream(
                    request.get_method(),
                    request.full_url,
                    content=cast(Any, request.data),
                    headers=dict(request.header_items()),
                ) as response:
                    headers = Message()
                    for key, value in response.headers.multi_items():
                        headers[key] = value
                    failed = not 200 <= response.status_code < 300
                    limit = ERROR_BODY_LIMIT if failed else maximum
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        remaining_bytes = limit + 1 - len(body)
                        body.extend(chunk[:remaining_bytes])
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


def urlopen(request: Request, *, timeout: float, maximum: int, follow_redirects: bool = False) -> BufferedResponse:
    """Urllib-compatible buffered response; its read() never waits on a socket.

    Run in an owned thread so the same synchronous entry point also works when
    called by a running event loop. Existing facade urlopen monkeypatchs stay at
    their current boundary and do not need to imitate httpx.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_http_deadline")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("invalid_response_size_limit")
    deadline = time.monotonic() + timeout
    admission = _ADMISSIONS
    if not admission.acquire(blocking=False):
        raise URLError("provider_transport_busy")
    completed = threading.Event()
    outcome: list[tuple[bool, Any]] = []

    def worker() -> None:
        try:
            response = asyncio.run(
                _fetch(request, deadline=deadline, maximum=maximum, follow_redirects=follow_redirects)
            )
            outcome.append((True, response))
        except BaseException as exc:  # noqa: BLE001 - hand the original exception back to the owning caller.
            outcome.append((False, exc))
        finally:
            admission.release()
            completed.set()

    try:
        threading.Thread(target=worker, name="autostop-bounded-http", daemon=True).start()
    except RuntimeError:
        admission.release()
        raise URLError("provider_transport_unavailable") from None
    if not completed.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        raise TimeoutError("provider_total_deadline_exceeded")
    succeeded, value = outcome[0]
    if not succeeded:
        raise value
    return value
