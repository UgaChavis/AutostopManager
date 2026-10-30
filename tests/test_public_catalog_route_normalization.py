"""Keep catalog attribution and robots checks ahead of HTTP path normalization."""

import json
from pathlib import Path
import socket
import sqlite3
import subprocess

import httpx
import pytest

from autostop_manager import elcats_catalog as catalog, public_catalog_http as transport

ORIGIN = "https://www.elcats.ru"
VW = {
    "id": "elcats_vw",
    "entry_url": ORIGIN + "/vw/",
    "brand": "Volkswagen",
    "provider": "elcats_catalog",
    "namespace": "elcats_epc",
}
HTML = (
    b"<table><tr><th>Part number</th><th>Description</th></tr><tr><td>DEMO200</td><td>Synthetic part</td></tr></table>"
)


@pytest.fixture(autouse=True)
def external_access_forbidden(monkeypatch):
    # Windows asyncio's socketpair uses loopback while building its event loop.
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def denied(*_args, **_kwargs):
        raise AssertionError("catalog route test attempted external access")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    for owner, name in (
        (socket, "getaddrinfo"),
        (socket, "create_connection"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, denied)


@pytest.fixture
def mock_http(monkeypatch):
    requests = []
    redirects = {}
    policy = [b"User-agent: *\nDisallow: /bmw/\n"]
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=policy[0])
        if request.url.path in redirects:
            return httpx.Response(302, headers={"Location": redirects[request.url.path]}, content=b"")
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, content=HTML)

    monkeypatch.setattr(transport, "_public_address", lambda *_args: "8.8.8.8")
    monkeypatch.setattr(
        transport.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    return requests, redirects, policy


def navigation(reader, entry=VW):
    return catalog._Navigation(reader, {"make": entry["brand"], "model": "Synthetic"}, None)


@pytest.mark.parametrize(
    "path",
    [
        "/vw/../bmw/Parts.aspx",
        "/vw/./Parts.aspx",
        "/vw/%2e%2e/bmw/Parts.aspx",
        "/vw/.%2E/bmw/Parts.aspx",
        "/vw/%2E./bmw/Parts.aspx",
        "/vw/%2e/Parts.aspx",
        "/vw/%2e%2e%2fbmw/Parts.aspx",
        "/vw/%2e%2e%5cbmw/Parts.aspx",
        "/vw/%5c..%5cbmw/Parts.aspx",
        "/vw/a%2f..%2f..%2fbmw/Parts.aspx",
    ],
)
@pytest.mark.parametrize("with_route_guard", [False, True], ids=["reader", "catalog-navigation"])
def test_ambiguous_absolute_path_is_rejected_before_robots_or_catalog_get(mock_http, path, with_route_guard):
    requests, _, _ = mock_http
    reader = transport.PublicCatalogReader()
    nav = navigation(reader)
    url = ORIGIN + path + "?Mdl=DEMO"
    with pytest.raises(transport.CatalogReadError, match=r"^unsafe_catalog_url$"):
        if with_route_guard:
            nav.page(VW, url, "list_parts")
        else:
            reader.read(url)
    assert requests == [] and reader.network_calls == 0
    assert nav.evidence == [] and reader.attempts == []


@pytest.mark.parametrize("path", ["/vw/../bmw/Parts.aspx", "/vw/%2e%2e/bmw/Parts.aspx"])
def test_absolute_redirect_cannot_cross_catalog_through_dot_segments(mock_http, path):
    requests, redirects, _ = mock_http
    redirects["/vw/Parts.aspx"] = ORIGIN + path
    reader = transport.PublicCatalogReader()
    nav = navigation(reader)
    with pytest.raises(transport.CatalogReadError, match=r"^unsafe_catalog_url$"):
        nav.page(VW, "Parts.aspx?Mdl=DEMO", "list_parts")
    assert [request.url.path for request in requests] == ["/robots.txt", "/vw/Parts.aspx"]
    assert reader.network_calls == 2 and nav.evidence == []


@pytest.mark.parametrize("relative", ["Parts.aspx", "./Parts.aspx", "sub/../Parts.aspx"])
def test_normal_relative_navigation_still_resolves_inside_selected_catalog(mock_http, relative):
    requests, _, _ = mock_http
    reader = transport.PublicCatalogReader()
    nav = navigation(reader)
    result = nav.page(VW, relative + "?Mdl=DEMO", "list_parts")
    assert result["rows"][0]["brand"] == "Volkswagen"
    assert nav.evidence[0]["locator"] == ORIGIN + "/vw/Parts.aspx?Mdl=DEMO"
    assert [request.url.path for request in requests] == ["/robots.txt", "/vw/Parts.aspx"]
    assert requests[-1].headers["Host"] == "www.elcats.ru"
    assert requests[-1].extensions["sni_hostname"] == "www.elcats.ru"


def test_relative_redirect_resolves_normally_before_route_guard(mock_http):
    requests, redirects, _ = mock_http
    redirects["/vw/Parts.aspx"] = "./sub/../Final.aspx?Mdl=DEMO"
    reader = transport.PublicCatalogReader()
    page = reader.read(ORIGIN + "/vw/Parts.aspx", route_guard=lambda url: catalog._route(VW, url))
    assert page.url == ORIGIN + "/vw/Final.aspx?Mdl=DEMO"
    assert [request.url.path for request in requests] == ["/robots.txt", "/vw/Parts.aspx", "/vw/Final.aspx"]


@pytest.mark.parametrize("target", ["../bmw/Parts.aspx", ORIGIN + "/bmw/Parts.aspx"])
def test_other_catalog_is_rejected_for_vw_but_available_when_selected(mock_http, target):
    requests, _, policy = mock_http
    policy[0] = b"User-agent: *\nDisallow:\n"
    reader = transport.PublicCatalogReader()
    with pytest.raises(transport.CatalogReadError, match=r"^unsafe_catalog_route$"):
        navigation(reader).page(VW, target, "list_parts")
    assert requests == []
    bmw = VW | {"id": "elcats_bmw", "brand": "BMW", "entry_url": ORIGIN + "/bmw/"}
    result = navigation(reader, bmw).page(bmw, "Parts.aspx?Mdl=DEMO", "list_parts")
    assert result["rows"][0]["brand"] == "BMW"
    assert [request.url.path for request in requests] == ["/robots.txt", "/bmw/Parts.aspx"]


def test_selected_catalog_still_obeys_robots_policy(mock_http):
    requests, _, _ = mock_http
    bmw = VW | {"id": "elcats_bmw", "brand": "BMW", "entry_url": ORIGIN + "/bmw/"}
    reader = transport.PublicCatalogReader()
    with pytest.raises(transport.CatalogReadError, match=r"^robots_disallowed$"):
        navigation(reader, bmw).page(bmw, "Parts.aspx?Mdl=DEMO", "list_parts")
    assert [request.url.path for request in requests] == ["/robots.txt"]


@pytest.mark.parametrize("path", ["Parts.aspx", "Part..Notes.aspx", "Part%2eNotes.aspx"])
def test_filename_dots_and_opaque_query_bytes_are_preserved(mock_http, path):
    requests, _, _ = mock_http
    query = "Key=a%2Fb%2B%3D%3D&Title=part%2E%2E&Note=%2E%2E%2Fbmw%2F&Blank=&Repeat=1&Repeat=2"
    url = ORIGIN + "/vw/" + path + "?" + query
    reader = transport.PublicCatalogReader()
    assert transport.safe_catalog_url(url) == url
    page = reader.read(url)
    assert page.url == url
    assert requests[-1].url.raw_path == ("/vw/" + path + "?" + query).encode()
    assert reader.read(url) is page and reader.network_calls == 2


def test_double_encoding_is_left_opaque_without_claiming_server_normalization(mock_http):
    requests, _, _ = mock_http
    url = ORIGIN + "/vw/%252e%252e%252fbmw/Parts.aspx?Mdl=DEMO"
    assert transport.safe_catalog_url(url) == url
    transport.PublicCatalogReader().read(url)
    # HTTPX preserves these bytes. MockTransport cannot prove a server's second decode.
    assert requests[-1].url.raw_path == b"/vw/%252e%252e%252fbmw/Parts.aspx?Mdl=DEMO"


def test_all_registered_entry_routes_remain_valid():
    registry = Path(__file__).resolve().parents[1] / "docs/agent/elcats_catalog_registry.json"
    entries = json.loads(registry.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 57
    for entry in entries:
        assert catalog._route(entry, entry["entry_url"]) == entry["entry_url"]
