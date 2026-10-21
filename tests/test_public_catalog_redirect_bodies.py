from __future__ import annotations

import httpx
import pytest

from autostop_manager import public_catalog_http as catalog


ROOT = "https://www.elcats.ru/vw/"


class TrackedBody(httpx.AsyncByteStream):
    def __init__(self, size=catalog.MAX_CATALOG_BYTES + 1):
        self.size = size
        self.read = False
        self.closed = False

    async def __aiter__(self):
        self.read = True
        yield b"x" * self.size

    async def aclose(self):
        self.closed = True


def use_mock_http(monkeypatch, handle):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(catalog, "_public_address", lambda *_args: "8.8.8.8")
    monkeypatch.setattr(
        catalog.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("redirect_robots", [False, True])
def test_redirect_body_is_closed_without_reading_before_next_request(monkeypatch, status, redirect_robots):
    body = TrackedBody()
    requests = []
    initial = "/robots.txt" if redirect_robots else "/vw/"
    destination = "/robots-final.txt" if redirect_robots else "/vw/final"

    def handle(request):
        path = request.url.path
        requests.append(path)
        if path == initial:
            return httpx.Response(status, headers={"Location": destination}, stream=body)
        if path == destination:
            assert body.closed, "close the redirect stream before dispatching the next request"
        if path in {"/robots.txt", "/robots-final.txt"}:
            return httpx.Response(404, content=b"")
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"final")

    use_mock_http(monkeypatch, handle)
    reader = catalog.PublicCatalogReader()
    page = reader.read(ROOT)
    assert page.text == "final"
    assert page.url == (ROOT if redirect_robots else "https://www.elcats.ru/vw/final")
    assert not body.read
    assert body.closed
    assert requests == (
        ["/robots.txt", "/robots-final.txt", "/vw/"] if redirect_robots else ["/robots.txt", "/vw/", "/vw/final"]
    )
    assert reader.network_calls == 3


@pytest.mark.parametrize(
    "destination,error",
    [
        ("https://www.japancats.ru/", "unsafe_catalog_redirect"),
        ("http://www.elcats.ru/vw/", "unsafe_catalog_redirect"),
        ("https://127.0.0.1/", "unsafe_catalog_url"),
    ],
)
def test_unsafe_redirect_is_rejected_without_reading_its_body(monkeypatch, destination, error):
    body = TrackedBody()
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, content=b"")
        return httpx.Response(302, headers={"Location": destination}, stream=body)

    use_mock_http(monkeypatch, handle)
    with pytest.raises(catalog.CatalogReadError, match=error):
        catalog.PublicCatalogReader().read(ROOT)
    assert requests == ["/robots.txt", "/vw/"]
    assert not body.read
    assert body.closed


@pytest.mark.parametrize("location", [None, "/vw/"])
def test_missing_location_or_redirect_loop_keeps_redirect_limit(monkeypatch, location):
    bodies = []

    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, content=b"")
        body = TrackedBody(size=8)
        bodies.append(body)
        return httpx.Response(302, headers={} if location is None else {"Location": location}, stream=body)

    use_mock_http(monkeypatch, handle)
    reader = catalog.PublicCatalogReader()
    with pytest.raises(catalog.CatalogReadError, match="catalog_redirect_limit"):
        reader.read(ROOT)
    assert len(bodies) == 4
    assert reader.network_calls == 5
    assert all(body.closed and not body.read for body in bodies)


def test_final_response_size_limit_still_closes_oversized_body(monkeypatch):
    body = TrackedBody(size=9)
    use_mock_http(monkeypatch, lambda _request: httpx.Response(200, stream=body))
    with pytest.raises(catalog.CatalogReadError, match="catalog_response_too_large"):
        catalog._http_get(ROOT, 1, 8)
    assert body.read
    assert body.closed
