"""Observe the catalog's owned body buffer without measuring decoder memory."""

from __future__ import annotations

import threading

import httpx
import pytest

from autostop_manager import public_catalog_http as catalog


URL = "https://www.elcats.ru/vw/"
MAXIMUM = 16


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    ("chunks", "expected_body"),
    [
        pytest.param((b"x" * 65536,), None, id="single-oversized-chunk"),
        pytest.param((b"a" * 15, b"x" * 65536), None, id="almost-full-then-oversized-chunk"),
        pytest.param((b"first-", b"middle", b"-end"), b"first-middle-end", id="exact-cap-in-several-chunks"),
    ],
)
def test_owned_buffer_budget_preserves_results_cleanup_and_admission(monkeypatch, chunks, expected_body):
    observed_buffers = []

    class ObservedBuffer(bytearray):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.high_water = len(self)
            observed_buffers.append(self)

        def extend(self, value):
            super().extend(value)
            self.high_water = max(self.high_water, len(self))

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    bodies = []
    clients = []
    transports = []

    def handle(_request):
        body = Body(chunks if not bodies else (b"ok",))
        bodies.append(body)
        return httpx.Response(200, stream=body)

    real_client = httpx.AsyncClient

    def client(**kwargs):
        mock_transport = Transport(handle)
        transports.append(mock_transport)
        result = real_client(transport=mock_transport, **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(catalog, "bytearray", ObservedBuffer, raising=False)
    monkeypatch.setattr(catalog, "_ADMISSIONS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(catalog, "_public_address", lambda *_args: "8.8.8.8")
    monkeypatch.setattr(catalog.httpx, "AsyncClient", client)

    if expected_body is None:
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_response_too_large$") as raised:
            catalog._bounded_get(URL, 3, MAXIMUM)
        assert raised.value.retryable is False
    else:
        assert catalog._bounded_get(URL, 3, MAXIMUM)[:2] == (200, expected_body)
    assert len(observed_buffers) == len(bodies) == len(clients) == 1
    assert bodies[0].closed and clients[0].is_closed and transports[0].closed

    # Exercise admission through another real bounded call, rather than
    # inferring release from a semaphore value or the caller's exception.
    assert catalog._bounded_get(URL, 3, MAXIMUM)[:2] == (200, b"ok")
    assert len(observed_buffers) == len(bodies) == len(clients) == 2
    assert all(body.closed for body in bodies)
    assert all(item.is_closed for item in clients)
    assert all(item.closed for item in transports)
    assert max(buffer.high_water for buffer in observed_buffers) <= MAXIMUM + 1
