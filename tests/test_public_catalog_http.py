from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from autostop_manager import public_catalog_http as transport


ROOT = "https://www.elcats.ru/vw/"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://localhost/",
        "https://elcats.ru.evil.com/",
        "https://user:secret@www.elcats.ru/",
        "https://www.elcats.ru:8080/",
        "https://www.elcats.ru/#x",
        "https://www.elcats.ru/\\evil",
        "https://www.elcats.ru/?VIN=x",
        "https://www.elcats.ru/?x=1HGCM82633A004352",
        "file:///etc/passwd",
        "https://www.elcats.ru/\n",
        "https://www.elcats.ru:" + "x",
    ],
)
def test_private_or_unregistered_routes_are_rejected(url):
    with pytest.raises(transport.CatalogReadError, match="unsafe_catalog_url"):
        transport.safe_catalog_url(url)


def test_bom_robots_disallow_stops_before_catalog_get(monkeypatch):
    requests = []

    def get(url, timeout, maximum):
        requests.append(url)
        return 200, b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n", {}

    monkeypatch.setattr(transport, "_bounded_get", get)
    reader = transport.PublicCatalogReader()
    with pytest.raises(transport.CatalogReadError, match="robots_disallowed"):
        reader.read(ROOT)
    assert requests == ["https://www.elcats.ru/robots.txt"]
    assert reader.network_calls == 1


@pytest.mark.parametrize(
    "status,body,error",
    [
        (403, b"", "robots_disallowed"),
        (500, b"", "robots_unavailable"),
        (200, b"<html>Login</html>", "robots_unavailable"),
        (200, b"\xff", "robots_unavailable"),
    ],
)
def test_uncertain_policy_fails_closed(monkeypatch, status, body, error):
    monkeypatch.setattr(transport, "_bounded_get", lambda *args: (status, body, {}))
    with pytest.raises(transport.CatalogReadError, match=error):
        transport.PublicCatalogReader().read(ROOT)


def test_404_policy_and_html_cache_count_actual_requests(monkeypatch):
    def get(url, timeout, maximum):
        return (
            (404, b"", {})
            if url.endswith("robots.txt")
            else (200, "Тормоза".encode("cp1251"), {"content-type": "text/html; charset=windows-1251"})
        )

    monkeypatch.setattr(transport, "_bounded_get", get)
    reader = transport.PublicCatalogReader(page_budget=2)
    assert reader.read(ROOT).text == "Тормоза"
    assert reader.read(ROOT).text == "Тормоза"
    assert reader.network_calls == 2


@pytest.mark.parametrize("destination", ["https://www.japancats.ru/", "http://www.elcats.ru/vw/", "https://127.0.0.1/"])
def test_redirect_is_rejected_before_destination_get(monkeypatch, destination):
    requests = []

    def get(url, timeout, maximum):
        requests.append(url)
        return (404, b"", {}) if url.endswith("robots.txt") else (302, b"", {"location": destination})

    monkeypatch.setattr(transport, "_bounded_get", get)
    with pytest.raises(transport.CatalogReadError):
        transport.PublicCatalogReader().read(ROOT)
    assert len(requests) == 2


def test_same_host_other_catalog_prefix_cannot_change_attribution(monkeypatch):
    requests = []

    def get(url, timeout, maximum):
        requests.append(url)
        return (404, b"", {}) if url.endswith("robots.txt") else (302, b"", {"location": "/bmw/"})

    def guard(url):
        if "/vw/" not in url:
            raise transport.CatalogReadError("unsafe_catalog_route")
        return url

    monkeypatch.setattr(transport, "_bounded_get", get)
    with pytest.raises(transport.CatalogReadError, match="unsafe_catalog_route"):
        transport.PublicCatalogReader().read(ROOT, route_guard=guard)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "status,error",
    [
        (401, "catalog_auth_required"),
        (429, "catalog_rate_limited"),
        (503, "catalog_provider_error"),
        (404, "catalog_http_error"),
    ],
)
def test_http_error_categories_and_receipt_are_stable(monkeypatch, status, error):
    monkeypatch.setattr(
        transport,
        "_bounded_get",
        lambda url, *args: (404, b"", {}) if url.endswith("robots.txt") else (status, b"", {}),
    )
    reader = transport.PublicCatalogReader()
    with pytest.raises(transport.CatalogReadError, match=error):
        reader.read(ROOT)
    assert reader.attempts[-1]["status"] == status


def test_budget_and_deadline_include_robots_reads(monkeypatch):
    monkeypatch.setattr(transport, "_bounded_get", lambda *args: (404, b"", {}))
    with pytest.raises(transport.CatalogReadError, match="catalog_page_budget_exceeded"):
        transport.PublicCatalogReader(page_budget=1).read(ROOT)
    with pytest.raises(transport.CatalogReadError, match="catalog_deadline_exceeded"):
        transport.PublicCatalogReader(deadline_seconds=0).read(ROOT)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.0.2.1"])
def test_dns_must_resolve_entirely_to_public_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(transport.CatalogReadError, match="unsafe_catalog_address"):
        transport._public_address("www.elcats.ru", 443)


def test_dns_pinning_preserves_tls_hostname_and_original_host(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, content=b"okay")

    real_client = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(transport, "_public_address", lambda *args: "8.8.8.8")
    monkeypatch.setattr(httpx, "Client", client)
    assert transport._http_get(ROOT, 1, 100)[1] == b"okay"
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["Host"] == "www.elcats.ru"
    assert seen[0].extensions["sni_hostname"] == "www.elcats.ru"


def test_bounded_wait_holds_admission_until_worker_finishes(monkeypatch):
    started, release = threading.Event(), threading.Event()
    semaphore = threading.BoundedSemaphore(1)

    def get(*args):
        started.set()
        release.wait(1)
        return 200, b"okay", {}

    monkeypatch.setattr(transport, "_ADMISSIONS", semaphore)
    monkeypatch.setattr(transport, "_http_get", get)
    try:
        with pytest.raises(transport.CatalogReadError, match="catalog_deadline_exceeded"):
            transport._bounded_get(ROOT, 0.01, 100)
        assert started.is_set()
        with pytest.raises(transport.CatalogReadError, match="catalog_transport_busy"):
            transport._bounded_get(ROOT, 0.01, 100)
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if semaphore.acquire(blocking=False):
            semaphore.release()
            break
        time.sleep(0.001)
    else:
        pytest.fail("worker admission was not released")


def test_actual_png_signature_and_size_not_mislabelled_jpeg_are_used(monkeypatch):
    image = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\x0dIHDR" + (82).to_bytes(4, "big") + (16).to_bytes(4, "big")
    monkeypatch.setattr(
        transport,
        "_bounded_get",
        lambda url, *args: (
            (404, b"", {}) if url.endswith("robots.txt") else (200, image, {"content-type": "image/jpeg"})
        ),
    )
    reader = transport.PublicCatalogReader()
    assert reader.read_number_image("https://ssangyong.exist.ru/PCode.ashx?Code=opaque").body == image
    assert reader.network_calls == 2
    with pytest.raises(transport.CatalogReadError, match="unsupported_number_image"):
        reader.read_number_image("https://ssangyong.exist.ru/Captcha.aspx")


def test_html_encoding_and_non_html_types_fail_closed(monkeypatch):
    assert transport._decode_html("Тормоза".encode("cp1251"), {}) == "Тормоза"
    with pytest.raises(transport.CatalogReadError, match="catalog_encoding_error"):
        transport._decode_html(b"test", {"content-type": "text/html;charset=no-such-encoding"})
    monkeypatch.setattr(
        transport,
        "_bounded_get",
        lambda url, *args: (
            (404, b"", {})
            if url.endswith("robots.txt")
            else (200, b"binary", {"content-type": "application/octet-stream"})
        ),
    )
    with pytest.raises(transport.CatalogReadError, match="catalog_content_type_error"):
        transport.PublicCatalogReader().read(ROOT)
