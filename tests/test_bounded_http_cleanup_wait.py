from __future__ import annotations

import asyncio
import threading
import time
from urllib.request import Request

import httpx
import pytest

from autostop_manager import bounded_http_read as bounded


@pytest.mark.parametrize("wait_for_cleanup", [False, True])
@pytest.mark.parametrize("failure", ["body_timeout", "header_observer", "header_selector"])
def test_cleanup_wait_is_opt_in_and_retains_capacity_until_actual_exit(monkeypatch, wait_for_cleanup, failure):
    admission = bounded.TrackedTransportAdmission(1)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    failures = []
    client = httpx.AsyncClient

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b"unused"

        async def aclose(self):
            entered.set()
            await asyncio.to_thread(release.wait, 5)

    def broken(*_args):
        raise ValueError("synthetic callback failure")

    async def handle(_request):
        return httpx.Response(200, stream=Body())

    monkeypatch.setattr(
        bounded.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs)
    )

    def call():
        try:
            bounded.urlopen(
                Request("http://127.0.0.1/synthetic"),
                timeout=0.3,
                maximum=20,
                admission=admission,
                wait_for_cleanup=wait_for_cleanup,
                on_response_headers=broken if failure == "header_observer" else None,
                response_maximum=broken if failure == "header_selector" else None,
            )
        except (TimeoutError, ValueError) as exc:
            failures.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=call)
    caller.start()
    try:
        assert entered.wait(2)
        assert returned.wait(0.5) is (not wait_for_cleanup)
        assert admission.pending_count == 1
        assert not admission.acquire()
    finally:
        release.set()
        caller.join(3)
    assert not caller.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], TimeoutError)
    # The default caller may finish first; wait for its actual worker as well.
    deadline = time.monotonic() + 2
    while admission.pending_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert admission.pending_count == 0


def test_response_observer_does_not_disable_requested_redirects(monkeypatch):
    client = httpx.AsyncClient
    observed, requests = [], []

    async def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/final"}, stream=httpx.ByteStream(b"unused"))
        return httpx.Response(200, stream=httpx.ByteStream(b"final"))

    monkeypatch.setattr(
        bounded.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs)
    )
    with bounded.urlopen(
        Request("http://127.0.0.1/redirect"),
        timeout=2,
        maximum=20,
        follow_redirects=True,
        preserve_error_status=True,
        on_response_headers=lambda status, _headers: observed.append(status),
        wait_for_cleanup=True,
    ) as response:
        assert response.read() == b"final"
    assert requests == ["/redirect", "/final"]
    assert observed == [302, 200]


@pytest.mark.parametrize("options", [{"on_response_headers": False}, {"wait_for_cleanup": 1}])
def test_invalid_options_do_not_acquire_capacity(options):
    admission = bounded.TrackedTransportAdmission(1)
    with pytest.raises(ValueError):
        bounded.urlopen(Request("http://127.0.0.1/unused"), timeout=1, maximum=20, admission=admission, **options)
    assert admission.pending_count == 0
