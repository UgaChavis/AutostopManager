"""Offline scoped-egress tests; all VINs are synthetic serial-zero fixtures."""

from __future__ import annotations

import io
import http.client
import json
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from autostop_manager import j1_fetch, j1_vin_network as network
from autostop_manager.j1_sources import classify_source

VIN = "Z94K241BBKR000000"
OTHER = "1HGCM82673A000000"


def scope(*engines: str, lifetime: float = 60) -> network.VinScope:
    return network.VinScope(VIN, "synthetic-job", time.time() + lifetime, engines or ("bing", "yahoo", "duckduckgo"))


@pytest.fixture(autouse=True)
def isolate_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # A forgotten mock must fail locally rather than issue live DNS or HTTP.
    def blocked(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("live_network_forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    j1_fetch._ROBOTS.clear()
    j1_fetch._HOST_NEXT.clear()


class Response:
    def __init__(self, status: int = 200, body: bytes = b"public", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {"Content-Type": "text/html"}

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers.items())

    def read(self, size: int) -> bytes:
        return self.body.read(size)

    read1 = read


def fake_transport(
    monkeypatch: pytest.MonkeyPatch, responses: list[Response], *, robots: bool = True
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Connection:
        sock = None

        def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
            self.record = {"host": host, "address": address, "port": port, "timeout": timeout}

        def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
            self.record.update(method=method, target=target, headers=headers)
            calls.append(self.record)

        def getresponse(self) -> Response:
            return responses.pop(0)

        def close(self) -> None:
            self.record["closed"] = True

    monkeypatch.setattr(network, "_public_address", lambda *_args: "93.184.216.34")
    monkeypatch.setattr(network, "_PinnedHTTP", Connection)
    monkeypatch.setattr(network, "_PinnedHTTPS", Connection)
    if robots:
        monkeypatch.setattr(network, "_robots_policy", lambda *_args: (True, 0.0))
    return calls


@pytest.mark.parametrize("vin", [VIN, "WVWZZZ1JZXW000000", "00000000000000000", "1HGCM826X3A000000"])
def test_normalization_checks_syntax_without_universal_checksum_or_wmi(vin: str) -> None:
    assert network.normalize_vin(" " + vin.lower() + " ") == vin


@pytest.mark.parametrize("vin", ["TESTVIN", "Z94K241BBKR00000O", "Z94-K241BBKR000000", "", None])
def test_invalid_vin_syntax(vin: Any) -> None:
    with pytest.raises(ValueError, match=r"^invalid_vin$"):
        network.normalize_vin(vin)


def test_scope_validates_selected_engines_and_accepts_empty_selection() -> None:
    with pytest.raises(ValueError, match="invalid_engines"):
        network.VinScope(VIN, "test", time.time() + 60, ("google",))
    with pytest.raises(ValueError, match="invalid_engines"):
        network.VinScope(VIN, "test", time.time() + 60, ("bing", "bing"))
    empty = network.VinScope(VIN, "test", time.time() + 60, ())
    result = network.search_vin(VIN, empty)
    assert result["result_class"] == "error"
    assert result["errors"] == [{"provider": "policy", "error": "no_selected_engines"}]


def test_exact_target_exception_does_not_change_generic_guards() -> None:
    authorized = scope()
    assert network.safe_query('"' + VIN + '" official manual', authorized)
    assert network.safe_query(quote(VIN, safe=""), authorized)
    assert network.safe_url("https://example.org/report/" + VIN + ".pdf", authorized)
    assert network.safe_url("https://example.org/?vin=" + quote(VIN), authorized)
    assert j1_fetch.contains_sensitive(VIN)
    assert not j1_fetch.public_url("https://example.org/?vin=" + VIN)
    assert network.safe_query("Hyundai engine workshop manual", authorized)


@pytest.mark.parametrize(
    "query",
    [
        OTHER,
        "1HG-CM826-73A-000000",
        "%31HGCM82673A000000",
        "%2531HGCM82673A000000",
        "1HG+CM826+73A+000000",
        "1HG\u200bCM82673A000000",
        "owner@example.org",
        "+44 7700 000000",
        "token=x",
        "password=secret",
        "!google " + VIN,
        "!!google " + VIN,
        "https://127.0.0.1/ " + VIN,
        "https://" + VIN + ".example.org/",
        "https://example.org/#" + VIN,
        VIN[:3] + "/" + VIN[3:],
    ],
)
def test_query_rejects_foreign_identifiers_contacts_secrets_bangs_and_unsafe_urls(query: str) -> None:
    assert not network.safe_query(query, scope())


def test_deep_encoding_is_not_a_privacy_bypass() -> None:
    encoded = "%31" + OTHER[1:]
    for _ in range(100):
        encoded = encoded.replace("%", "%25")
    assert not network.safe_query(encoded, scope())
    assert not network.safe_url("https://example.org/?vin=" + encoded, scope())


@pytest.mark.parametrize(
    "url",
    [
        "https://" + VIN + ".example.org/report",
        "https://example.org/#" + VIN,
        "https://example.org/" + OTHER,
        "https://example.org/?vin=%2531HGCM82673A000000",
        "https://example.org/1HG%2FCM826%2F73A%2F000000",
        "https://example.org/?vin=1HG+CM826+73A+000000",
        "https://example.org/owner%40example.org",
        "https://example.org/?token=x",
        "https://user:password@example.org/report",
        "https://127.0.0.1/report",
        "https://example.internal/report",
        "https://example.org/%0a" + OTHER,
        "https://api.example.org/vin/" + VIN,
        "https://example.org/api/v1/decode?vin=" + VIN,
    ],
)
def test_unsafe_url_cases(url: str) -> None:
    assert not network.safe_url(url, scope())


def test_expiry_rejects_query_url_and_request_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    expired = scope(lifetime=-1)
    assert not network.safe_query(VIN, expired)
    assert not network.safe_url("https://example.org/", expired)
    assert network.search_vin(VIN, expired)["errors"][0]["error"] == "scope_expired"
    with pytest.raises(ValueError, match=r"^scope_expired$"):
        network.request_vin("https://example.org/", expired, max_bytes=100)


def test_public_dns_is_pinned_and_body_is_kept_only_for_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (VIN + " " + OTHER).encode()
    calls = fake_transport(
        monkeypatch,
        [Response(body=body, headers={"Content-Type": "text/html", "Set-Cookie": "token=do-not-export"})],
    )
    url = "https://example.org/report/" + VIN
    status, headers, raw, final = network.request_vin(url, scope(), max_bytes=100)
    assert (status, raw, final) == (200, body, url)
    assert headers == {"content-type": "text/html"}
    assert calls[0]["address"] == "93.184.216.34"
    assert calls[0]["method"] == "GET"
    assert "Cookie" not in calls[0]["headers"] and "Authorization" not in calls[0]["headers"]
    assert calls[0]["closed"] is True


@pytest.mark.parametrize("location", ["/" + OTHER, "/%2531HGCM82673A000000", "https://127.0.0.1/report"])
def test_redirect_rechecks_privacy_before_second_connection(monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    calls = fake_transport(monkeypatch, [Response(302, headers={"Location": location})])
    with pytest.raises(ValueError, match=r"^unsafe_redirect$"):
        network.request_vin("https://example.org/" + VIN, scope(), max_bytes=100)
    assert len(calls) == 1


def test_scoped_redirect_is_allowed_and_checked_against_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [Response(302, headers={"Location": "/" + VIN}), Response(body=b"record")])
    checked: list[str] = []

    def robots(url: str, *_args: object) -> tuple[bool, float]:
        checked.append(url)
        return True, 0.0

    monkeypatch.setattr(network, "_robots_policy", robots)
    result = network.request_vin("https://example.org/report", scope(), max_bytes=100)
    assert result[2] == b"record"
    assert checked == ["https://example.org/report", "https://example.org/" + VIN]
    assert len(calls) == 2


def test_scope_expiry_during_redirect_blocks_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(network.time, "time", lambda: clock[0])
    authorized = network.VinScope(VIN, "test", 101)

    class ExpiringResponse(Response):
        def getheaders(self) -> list[tuple[str, str]]:
            clock[0] = 102
            return [("Location", "/" + VIN)]

    calls = fake_transport(monkeypatch, [ExpiringResponse(302)])
    with pytest.raises(ValueError, match=r"^scope_expired$"):
        network.request_vin("https://example.org/report", authorized, max_bytes=100)
    assert len(calls) == 1


@pytest.mark.parametrize("raises", [False, True])
def test_active_check_denial_or_failure_prevents_network_io(monkeypatch: pytest.MonkeyPatch, raises: bool) -> None:
    def denied() -> bool:
        if raises:
            raise OSError("private cancellation state " + VIN)
        return False

    authorized = network.VinScope(VIN, "test", time.time() + 60, active_check=denied)
    calls = fake_transport(monkeypatch, [])

    def forbidden(*_args: object) -> Any:
        raise AssertionError("provider_called_after_cancellation")

    monkeypatch.setattr(network, "_searxng_search", forbidden)
    monkeypatch.setattr(network, "_duckduckgo_search", forbidden)
    assert network.safe_query(VIN, authorized) is False
    assert network.safe_url("https://example.org/" + VIN, authorized) == ""
    with pytest.raises(ValueError, match=r"^scope_expired$"):
        network.request_vin("https://example.org/report", authorized, max_bytes=100)
    result = network.search_vin(VIN, authorized)
    assert result["errors"] == [{"provider": "policy", "error": "scope_expired"}]
    assert result["providers"] == [] and result["results"] == [] and calls == []
    assert VIN not in json.dumps(result)


def test_active_check_cancelled_during_redirect_headers_blocks_second_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = [True]
    authorized = network.VinScope(VIN, "test", time.time() + 60, active_check=lambda: active[0])

    class CancellingResponse(Response):
        def getheaders(self) -> list[tuple[str, str]]:
            active[0] = False
            return [("Location", "/" + VIN)]

    calls = fake_transport(monkeypatch, [CancellingResponse(302)])
    with pytest.raises(ValueError, match=r"^scope_expired$"):
        network.request_vin("https://example.org/report", authorized, max_bytes=100)
    assert len(calls) == 1 and calls[0]["target"] == "/report" and calls[0]["closed"]


def test_active_check_cancelled_after_primary_provider_skips_remaining_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = [True]
    recipients: list[str] = []
    authorized = network.VinScope(VIN, "test", time.time() + 60, active_check=lambda: active[0])

    def cancels(_query: str, _scope: network.VinScope, engine: str) -> list[dict[str, Any]]:
        recipients.append(engine)
        active[0] = False
        return [{"url": "https://example.org/report", "title": VIN}]

    def forbidden(*_args: object) -> Any:
        raise AssertionError("fallback_called_after_cancellation")

    monkeypatch.setattr(network, "_searxng_search", cancels)
    monkeypatch.setattr(network, "_duckduckgo_search", forbidden)
    result = network.search_vin(VIN, authorized)
    assert recipients == ["bing"]
    assert result["ok"] is False and result["result_class"] == "error" and result["results"] == []
    assert result["errors"] == [{"provider": "policy", "error": "scope_expired"}]
    assert result["providers"][1:] == [
        {"provider": "yahoo", "outcome": "skipped", "reason": "scope_expired"},
        {"provider": "duckduckgo", "outcome": "skipped", "reason": "scope_expired"},
    ]


def test_redirect_robots_denial_blocks_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [Response(302, headers={"Location": "https://other.example.org/report"})])
    monkeypatch.setattr(network, "_robots_policy", lambda url, *_args: ("other." not in url, 0))
    with pytest.raises(ValueError, match=r"^redirect_robots_disallowed$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100)
    assert len(calls) == 1


def test_robots_disallow_and_unavailable_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [Response(body=b"User-agent: *\nDisallow: /report\n")], robots=False)
    with pytest.raises(ValueError, match=r"^robots_disallowed$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100)
    assert [call["target"] for call in calls] == ["/robots.txt"]


def test_dns_mixed_private_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_transport(monkeypatch, [])
    monkeypatch.setattr(network, "_public_address", j1_fetch._public_address)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match=r"^unsafe_dns_answer$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100)


def test_dns_has_total_deadline_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [])

    def slow_dns(*_args: object) -> str:
        time.sleep(0.08)
        return "93.184.216.34"

    monkeypatch.setattr(network, "_public_address", slow_dns)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^fetch_timeout$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100, timeout=0.01)
    assert time.monotonic() - started < 0.3
    assert calls == []


def test_stream_size_and_content_encoding_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_transport(monkeypatch, [Response(body=b"x" * 101)])
    with pytest.raises(ValueError, match=r"^document_too_large$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100)
    fake_transport(monkeypatch, [Response(headers={"Content-Encoding": "gzip"})])
    with pytest.raises(ValueError, match=r"^unsupported_content_encoding$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100)


def test_large_allowance_requires_registry_tier_at_every_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [Response(302, headers={"Location": "https://example.org/manual.pdf"})])
    with pytest.raises(ValueError, match=r"^large_document_source_untrusted$"):
        network.request_vin("https://hyundai.ru/manual.pdf", scope(), max_bytes=24 * 1024 * 1024)
    assert len(calls) == 1
    with pytest.raises(ValueError, match=r"^large_document_source_untrusted$"):
        network.request_vin("https://example.org/manual.pdf", scope(), max_bytes=8_000_001)
    with pytest.raises(ValueError, match=r"^invalid_max_bytes$"):
        network.request_vin("https://hyundai.ru/manual.pdf", scope(), max_bytes=24 * 1024 * 1024 + 1)


@pytest.mark.parametrize("domain", ["hyundai.ru", "hyundai.com", "hondanews.com"])
def test_explicit_oem_registry_rules(domain: str) -> None:
    classified = classify_source("https://www." + domain + "/manual")
    assert (classified.source_class, classified.source_tier) == ("official_registry", "A")
    assert classified.source_basis.startswith("registry:official:")
    assert classify_source("https://" + domain + ".example.org/manual").source_tier != "A"


def test_search_preserves_exact_target_and_redacts_foreign_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    recipients: list[str] = []

    def search(_query: str, _scope: network.VinScope, engine: str) -> list[dict[str, Any]]:
        recipients.append(engine)
        return [
            {"url": "https://hyundai.ru/manual", "title": VIN, "content": OTHER + " owner@example.org token=x"},
            {"url": "https://example.org/" + OTHER, "title": "foreign result"},
        ]

    monkeypatch.setattr(network, "_searxng_search", search)
    result = network.search_vin(VIN, scope("bing", "yahoo"))
    assert result["result_class"] == "results" and result["ok"] is True
    assert recipients == ["bing", "yahoo"]
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["title"] == VIN and row["engines"] == ["bing", "yahoo"]
    assert OTHER not in json.dumps(result) and "owner@example.org" not in json.dumps(result)
    assert "token=x" not in json.dumps(result)
    assert row["source_tier"] == "A"


@pytest.mark.parametrize(
    "text",
    [
        "%" + "31HGCM82673A000000",
        "%2531HGCM82673A000000",
        "&#49;HGCM82673A000000",
        "1HG\u200bCM82673A000000",
        "1HG\u2060CM82673A000000",
        "<b>" + OTHER + "</b>",
        "token&#61;private-value owner&#64;example.org",
        "password%3Dprivate-value owner%40example.org",
    ],
)
def test_search_metadata_decodes_before_redaction(text: str) -> None:
    clean = network._clean_metadata(text, scope(), 600)
    assert OTHER not in clean
    assert "private-value" not in clean and "owner@example.org" not in clean
    assert "\u200b" not in clean and "\u2060" not in clean
    assert "[redacted" in clean


@pytest.mark.parametrize("value", [{"token": "private-value"}, ["private-value"], 17, None])
def test_metadata_nontext_values_are_not_serialized(value: Any) -> None:
    assert network._clean_metadata(value, scope(), 600) == ""


def test_metadata_mask_literal_does_not_fabricate_target_evidence() -> None:
    clean = network._clean_metadata("literal [scoped_vin] marker", scope(), 600)
    assert clean == "literal [scoped_vin] marker" and VIN not in clean
    clean = network._clean_metadata("[scoped_vin] " + VIN, scope(), 600)
    assert clean == "[scoped_vin] " + VIN


def test_expiry_during_header_sanitization_prevents_export(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [time.time()]
    authorized = network.VinScope(VIN, "test", clock[0] + 60, ("bing",))
    monkeypatch.setattr(network.time, "time", lambda: clock[0])
    original = network._clean_metadata

    def expires(value: object, scoped: network.VinScope, limit: int) -> str:
        result = original(value, scoped, limit)
        clock[0] = authorized.expires_at + 1
        return result

    monkeypatch.setattr(network, "_clean_metadata", expires)
    calls = fake_transport(monkeypatch, [Response(body=VIN.encode())])
    with pytest.raises(ValueError, match=r"^scope_expired$"):
        network.request_vin("https://example.org/report", authorized, max_bytes=100)
    assert len(calls) == 1 and calls[0]["closed"]


def test_total_deadline_interrupts_headers_and_preserves_timeout_class(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupted = threading.Event()

    class Socket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def shutdown(self, _how: int) -> None:
            interrupted.set()

        def close(self) -> None:
            pass

    class Connection:
        sock = Socket()

        def __init__(self, *_args: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> Any:
            assert interrupted.wait(0.5), "watchdog_did_not_interrupt_headers"
            raise http.client.RemoteDisconnected("private source " + VIN)

        def close(self) -> None:
            pass

    monkeypatch.setattr(network, "_robots_policy", lambda *_args: (True, 0.0))
    monkeypatch.setattr(network, "_public_address", lambda *_args: "93.184.216.34")
    monkeypatch.setattr(network, "_PinnedHTTPS", Connection)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^fetch_timeout$"):
        network.request_vin("https://example.org/report", scope(), max_bytes=100, timeout=0.02)
    assert interrupted.is_set() and time.monotonic() - started < 0.3


def test_search_exact_empty_and_transport_error_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "_searxng_search", lambda *_args: [])
    empty = network.search_vin(VIN, scope("bing"))
    assert (empty["ok"], empty["result_class"], empty["errors"]) == (True, "empty", [])

    def failed(*_args: object) -> Any:
        raise OSError("sensitive URL " + VIN)

    monkeypatch.setattr(network, "_searxng_search", failed)
    result = network.search_vin(VIN, scope("bing"))
    assert (result["ok"], result["result_class"]) == (False, "error")
    assert result["errors"] == [{"provider": "bing", "error": "transport_error"}]
    assert VIN not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_shared_searxng_auth_quota_error_skips_sibling(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    recipients: list[str] = []

    def denied(_query: str, _scope: network.VinScope, engine: str) -> Any:
        recipients.append(engine)
        raise network._http_error(status)

    monkeypatch.setattr(network, "_searxng_search", denied)
    result = network.search_vin(VIN, scope("bing", "yahoo"))
    assert recipients == ["bing"]
    assert result["errors"][0]["http_status"] == status
    assert result["providers"][1]["outcome"] == "skipped"


def test_ddg_is_fallback_only_and_requires_selected_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(network, "_searxng_search", lambda *_args: [])

    def ddg(*_args: object) -> list[dict[str, Any]]:
        calls.append("duckduckgo")
        return [{"url": "https://example.org/report", "title": VIN}]

    monkeypatch.setattr(network, "_duckduckgo_search", ddg)
    assert network.search_vin(VIN, scope("bing"))["result_class"] == "empty"
    assert calls == []
    assert network.search_vin(VIN, scope("bing", "duckduckgo"))["result_class"] == "results"
    assert calls == ["duckduckgo"]
    monkeypatch.setattr(network, "_searxng_search", ddg)
    result = network.search_vin(VIN, scope("bing", "duckduckgo"))
    assert calls == ["duckduckgo", "duckduckgo"]
    assert result["providers"][-1]["reason"] == "primary_results"


@pytest.mark.parametrize(
    "recipient",
    [
        "https://example.org/search",
        "https://duckduckgo.com.example.org/search",
        "http://html.duckduckgo.com/html/",
        "https://html.duckduckgo.com:80/html/",
        "https://lite.duckduckgo.com/html/",
    ],
)
def test_ddg_redirect_recipient_is_checked_before_robots_dns_or_get(
    monkeypatch: pytest.MonkeyPatch, recipient: str
) -> None:
    calls = fake_transport(
        monkeypatch,
        [
            Response(302, headers={"Location": recipient + "?q=" + VIN}),
            Response(body=b'<div class="no-results">Empty</div>'),
        ],
    )
    dns: list[str] = []
    robots: list[str] = []

    def resolve(host: str, _port: int) -> str:
        dns.append(host)
        return "93.184.216.34"

    def policy(url: str, *_args: Any) -> tuple[bool, float]:
        robots.append(url)
        return True, 0.0

    monkeypatch.setattr(network, "_public_address", resolve)
    monkeypatch.setattr(network, "_robots_policy", policy)
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["errors"] == [{"provider": "duckduckgo", "error": "search_recipient_disallowed"}]
    assert not result["ok"] and result["result_class"] == "error"
    assert dns == ["html.duckduckgo.com"] and len(robots) == 1 and len(calls) == 1
    assert VIN not in json.dumps(result)


@pytest.mark.parametrize("recipient", ["html.duckduckgo.com", "duckduckgo.com:443", "www.duckduckgo.com"])
def test_ddg_fixed_https_redirects_keep_robots_and_scoped_query(
    monkeypatch: pytest.MonkeyPatch, recipient: str
) -> None:
    location = "https://" + recipient + "/html/?q=" + VIN
    calls = fake_transport(
        monkeypatch,
        [
            Response(302, headers={"Location": location}),
            Response(body=b'<a class="result__a" href="https://example.org/manual">Manual</a>'),
        ],
    )
    checked: list[str] = []

    def policy(url: str, *_args: Any) -> tuple[bool, float]:
        checked.append(url)
        return True, 0.0

    monkeypatch.setattr(network, "_robots_policy", policy)
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] and result["errors"] == []
    assert [row["url"] for row in result["results"]] == ["https://example.org/manual"]
    assert len(calls) == 2 and calls[1]["host"] == urlsplit(location).hostname
    assert all(VIN in call["target"] for call in calls)
    assert checked == ["https://html.duckduckgo.com/html/?q=" + VIN, location]


def test_search_recipient_restriction_does_not_limit_scoped_static_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    location = "https://example.net/document/" + VIN
    calls = fake_transport(
        monkeypatch, [Response(302, headers={"Location": location}), Response(body=b"Public document")]
    )
    result = network.request_vin("https://example.org/document/" + VIN, scope("duckduckgo"), max_bytes=100)
    assert result[2:] == (b"Public document", location)
    assert [call["host"] for call in calls] == ["example.org", "example.net"]
    assert all(VIN in call["target"] for call in calls)


def test_search_recipient_guard_requires_selected_engine_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [])
    with pytest.raises(ValueError, match=r"^search_recipient_disallowed$"):
        network.request_vin(
            "https://html.duckduckgo.com/html/?q=" + VIN,
            scope("bing"),
            max_bytes=100,
            search_engine="duckduckgo",
        )
    assert calls == []


def test_ddg_redirect_keeps_robots_denial_before_followup_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [Response(302, headers={"Location": "/blocked?q=" + VIN})])
    monkeypatch.setattr(network, "_robots_policy", lambda url, *_args: ("/blocked" not in url, 0.0))
    with pytest.raises(ValueError, match=r"^redirect_robots_disallowed$"):
        network._duckduckgo_search(VIN, scope("duckduckgo"))
    assert len(calls) == 1


def test_ddg_search_keeps_private_dns_denial_before_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(monkeypatch, [])
    monkeypatch.setattr(network, "_public_address", lambda *_args: "127.0.0.1")
    with pytest.raises(ValueError, match=r"^unsafe_dns_answer$"):
        network._duckduckgo_search(VIN, scope("duckduckgo"))
    assert calls == []


@pytest.mark.parametrize("guard", ["deadline", "expiry", "cancel"])
def test_ddg_redirect_keeps_deadline_expiry_and_cancellation_guards(
    monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    clock = [100.0]
    active = [True]
    authorized = network.VinScope(VIN, "test", time.time() + 60, ("duckduckgo",), lambda: active[0])
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])

    class StoppingResponse(Response):
        def getheaders(self) -> list[tuple[str, str]]:
            if guard == "deadline":
                clock[0] += 11
            elif guard == "expiry":
                monkeypatch.setattr(network.time, "time", lambda: authorized.expires_at + 1)
            else:
                active[0] = False
            return [("Location", "/followup?q=" + VIN)]

    calls = fake_transport(monkeypatch, [StoppingResponse(302)])
    expected = "fetch_timeout" if guard == "deadline" else "scope_expired"
    with pytest.raises((TimeoutError, ValueError), match="^" + expected + "$"):
        network._duckduckgo_search(VIN, authorized)
    assert len(calls) == 1 and calls[0]["closed"]


def test_ddg_robots_redirect_keeps_fixed_https_recipient_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_transport(
        monkeypatch,
        [Response(302, headers={"Location": "http://html.duckduckgo.com/robots.txt"}), Response(404)],
        robots=False,
    )
    with pytest.raises(ValueError, match=r"^robots_disallowed$"):
        network._duckduckgo_search(VIN, scope("duckduckgo"))
    assert len(calls) == 1 and calls[0]["target"] == "/robots.txt"


def test_searxng_loopback_request_has_only_selected_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "http://127.0.0.1:8890")
    calls = fake_transport(monkeypatch, [Response(body=b'{"results": []}')])
    assert network._searxng_search('"' + VIN + '"', scope("bing"), "bing") == []
    request = calls[0]
    parsed = parse_qs(urlsplit(request["target"]).query)
    assert parsed["q"] == ['"' + VIN + '"'] and parsed["engines"] == ["bing"]
    assert request["host"] == request["address"] == "127.0.0.1"
    assert request["port"] == 8890
    with pytest.raises(ValueError, match=r"^unsafe_query$"):
        network._searxng_search(VIN, scope("bing"), "yahoo")


@pytest.mark.parametrize("body", [b"not JSON", b"{}", b'{"results": null}'])
def test_searxng_malformed_payload_is_error_not_empty(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "http://127.0.0.1:8890")
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("bing"))
    assert result["result_class"] == "error" and result["errors"][0]["error"] == "parse_failed"


@pytest.mark.parametrize("field", ["error", "errors", "unresponsive_engines"])
def test_searxng_embedded_failure_is_error_not_empty(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "http://127.0.0.1:8890")
    fake_transport(monkeypatch, [Response(body=json.dumps({"results": [], field: ["upstream failure"]}).encode())])
    result = network.search_vin(VIN, scope("bing"))
    assert result["result_class"] == "error"
    assert result["errors"] == [{"provider": "bing", "error": "engine_unavailable"}]


def test_ddg_parser_does_not_attach_unsafe_result_snippet_to_safe_source() -> None:
    parser = network._DDGLinks(scope())
    parser.feed(
        '<a class="result__a" href="https://example.org/safe">safe source</a>'
        '<div class="result__snippet">safe snippet</div>'
        '<a class="result__a" href="https://example.org/' + OTHER + '">foreign source</a>'
        '<div class="result__snippet">foreign snippet</div>'
    )
    assert len(parser.rows) == 1
    assert parser.rows[0]["snippet"] == "safe snippet"


@pytest.mark.parametrize(
    "body",
    [
        b"not HTML",
        b"<html><body><h1>Service temporarily unavailable</h1></body></html>",
        b'<html><body><form id="consent">Accept cookies to continue</form></body></html>',
        b'<html><div class="results" id="links"></div></html>',
        b'<html><div class="result--no-result-placeholder">No results found</div></html>',
        b'<html><!-- <div class="no-results">No results found</div> --></html>',
        b'<a class="result__a" href="https://example.org/report">Incomplete result',
        b'<a class="result__a">Missing result URL</a>',
    ],
)
def test_ddg_unknown_or_incomplete_http_200_is_error_not_empty(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    calls = fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert (result["ok"], result["result_class"], result["results"]) == (False, "error", [])
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]
    assert result["providers"][0]["outcome"] == "error" and len(calls) == 1


def _synthetic_ddg_html(monkeypatch: pytest.MonkeyPatch, body: str) -> dict[str, Any]:
    fake_transport(monkeypatch, [Response(body=body.encode())])
    authorized = network.VinScope(OTHER, "synthetic-html-review", time.time() + 60, ("duckduckgo",))
    return network.search_vin(OTHER, authorized)


@pytest.mark.parametrize(
    "marker",
    [
        '<input type="hidden" class="no-results">',
        '<meta class="no-results">',
        '<img class="no-results">',
        '<br class="result--no-result">',
        '<textarea class="no-results">Control value</textarea>',
    ],
)
def test_ddg_intrinsic_nontext_markers_do_not_turn_outage_into_empty(
    monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    result = _synthetic_ddg_html(monkeypatch, "<html><body>" + marker + "<h1>Unavailable</h1></body></html>")
    assert not result["ok"] and result["results"] == [] and result["result_class"] == "error"
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


@pytest.mark.parametrize("tag", ["div", "span", "p", "strong"])
def test_ddg_visible_text_markers_still_recognize_empty(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    result = _synthetic_ddg_html(monkeypatch, f'<{tag} class="no-results">No results found</{tag}>')
    assert result["ok"] and result["result_class"] == "empty" and result["results"] == [] and result["errors"] == []


@pytest.mark.parametrize("tag", ["iframe", "noembed", "noframes", "canvas", "datalist", "dialog", "audio", "video"])
def test_ddg_intrinsic_hidden_fallback_does_not_enter_snippet(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    body = (
        '<a class="result__a" href="https://example.org/manual">Public title</a>'
        f'<div class="result__snippet">Public <{tag}>hidden fallback</{tag}>tail</div>'
    )
    result = _synthetic_ddg_html(monkeypatch, body)
    assert result["ok"] and result["errors"] == [] and len(result["results"]) == 1
    assert result["results"][0]["snippet"] == "Public tail"


def test_ddg_closed_details_marker_is_not_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    body = '<details><summary>Search options</summary><div class="no-results">Hidden empty</div></details><h1>Unavailable</h1>'
    result = _synthetic_ddg_html(monkeypatch, body)
    assert not result["ok"] and result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


@pytest.mark.parametrize("tag", ["template", "iframe", "script", "style"])
def test_ddg_html_nonvoid_slash_does_not_release_hidden_markers(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    body = (
        f'<{tag}/><div class="no-results">Hidden empty</div>'
        '<a class="result__a" href="https://example.org/hidden">Hidden result</a>'
        f"</{tag}><p>Visible tail</p>"
    )
    result = _synthetic_ddg_html(monkeypatch, body)
    assert not result["ok"] and result["results"] == []
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_foreign_integration_point_does_not_release_template_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '<math><mtext><template/><div class="no-results">Hidden empty</div>'
        '<a class="result__a" href="https://example.org/hidden">Hidden result</a>'
        "</template></mtext></math><h1>Unavailable</h1>"
    )
    result = _synthetic_ddg_html(monkeypatch, body)
    assert not result["ok"] and result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


@pytest.mark.parametrize("tag", ["script", "style", "iframe"])
def test_ddg_slash_rawtext_keeps_visible_result_after_matching_end(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    body = f'<{tag}/><{tag}></{tag}><a class="result__a" href="https://example.org/visible">Visible title</a>'
    result = _synthetic_ddg_html(monkeypatch, body)
    assert result["ok"] and result["errors"] == []
    assert [row["url"] for row in result["results"]] == ["https://example.org/visible"]


@pytest.mark.parametrize(
    "style,visible", [('style="display:none" style=""', False), ('style="" style="display:none"', True)]
)
def test_ddg_duplicate_style_keeps_first_visibility(monkeypatch: pytest.MonkeyPatch, style: str, visible: bool) -> None:
    body = f'<a class="result__a" href="https://example.org/manual" {style}>Title</a>'
    result = _synthetic_ddg_html(monkeypatch, body)
    assert len(result["results"]) == int(visible)
    assert result["ok"] is visible


@pytest.mark.parametrize(
    "classes,is_result",
    [('class="result__advert" class="result__a"', False), ('class="result__a" class="result__advert"', True)],
)
def test_ddg_duplicate_class_keeps_first_classification(
    monkeypatch: pytest.MonkeyPatch, classes: str, is_result: bool
) -> None:
    body = '<div class="no-results">Empty</div>' + f'<a {classes} href="https://example.org/manual">Title</a>'
    result = _synthetic_ddg_html(monkeypatch, body)
    assert result["ok"] and result["errors"] == []
    assert len(result["results"]) == int(is_result)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("https://example.org/first", "https://example.org/second", ["https://example.org/first"]),
        ("https://127.0.0.1/private", "https://example.org/second", []),
        ("https://example.org/first", "https://127.0.0.1/private", ["https://example.org/first"]),
    ],
)
def test_ddg_duplicate_href_filters_only_first_destination(
    monkeypatch: pytest.MonkeyPatch, first: str, second: str, expected: list[str]
) -> None:
    result = _synthetic_ddg_html(monkeypatch, f'<a class="result__a" href="{first}" href="{second}">Title</a>')
    assert result["ok"] and result["errors"] == []
    assert [row["url"] for row in result["results"]] == expected


def test_ddg_textarea_pseudo_tags_are_literal_control_value(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '<textarea><a class="result__a" href="https://example.org/control">Control</a>'
        '<div class="no-results">Control empty</div></textarea><h1>Unavailable</h1>'
    )
    result = _synthetic_ddg_html(monkeypatch, body)
    assert not result["ok"] and result["results"] == []
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_textarea_control_value_does_not_enter_result_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '<a class="result__a" href="https://example.org/manual">Public <textarea>Control title</textarea>title</a>'
        '<div class="result__snippet">Public <textarea>Control snippet</textarea>tail</div>'
    )
    result = _synthetic_ddg_html(monkeypatch, body)
    assert result["ok"] and result["errors"] == [] and len(result["results"]) == 1
    assert result["results"][0]["title"] == "Public title"
    assert result["results"][0]["snippet"] == "Public tail"


@pytest.mark.parametrize("classes", ["result__advert", "not-result__a", "result__a-placeholder"])
def test_ddg_no_results_does_not_export_anchors_with_similar_classes(
    monkeypatch: pytest.MonkeyPatch, classes: str
) -> None:
    body = (
        f'<div class="no-results">No results</div><a class="{classes}" href="https://example.org/advert">Advert</a>'
    ).encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] and result["result_class"] == "empty"
    assert result["results"] == [] and result["errors"] == []


@pytest.mark.parametrize(
    "incomplete",
    ['<a class="result__a">Missing URL</a>', '<a class="result__a" href="https://example.org/manual">Unfinished'],
)
def test_ddg_incomplete_anchor_cannot_lend_result_state_to_unrelated_link(
    monkeypatch: pytest.MonkeyPatch, incomplete: str
) -> None:
    body = (incomplete + '<a class="result__advert" href="https://example.org/advert">Advert</a>').encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert not result["ok"] and result["results"] == []
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


@pytest.mark.parametrize(
    "opening,closing",
    [
        ("<template>", "</template>"),
        ("<noscript>", "</noscript>"),
        ("<svg>", "</svg>"),
        ("<script>", "</script>"),
        ("<style>", "</style>"),
        ("<div hidden>", "</div>"),
        ('<div aria-hidden="true">', "</div>"),
        ('<div style="DISPLAY: none">', "</div>"),
        ('<div style="visibility: hidden">', "</div>"),
    ],
)
def test_ddg_hidden_markers_and_links_are_parse_failure_not_empty(
    monkeypatch: pytest.MonkeyPatch, opening: str, closing: str
) -> None:
    body = (
        "<html>" + opening + '<div class="no-results">No results</div>'
        '<a class="result__a" href="https://example.org/template">Template link</a>'
        + closing
        + "<body><h1>Service unavailable</h1></body></html>"
    ).encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert not result["ok"] and result["result_class"] == "error" and result["results"] == []
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_head_metadata_does_not_recognize_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'<html><head><meta class="no-results"></head><body>Service unavailable</body></html>'
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert not result["ok"] and result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_unmatched_ends_do_not_expose_template_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        b'<html><div><template></div><div class="no-results">No results</div>'
        b'<a class="result__a" href="https://example.org/hidden">Hidden</a></template></div>'
        b"<body>Service unavailable</body></html>"
    )
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert not result["ok"] and result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_visible_result_text_excludes_hidden_nodes_and_preserves_nested_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        b"<html><head><title>Public search</title><body>"
        b'<a class="extra result__a" href="https://example.org/manual">Public <span hidden>secret title</span>title</a>'
        b'<div class="result__snippet">Public <div hidden>secret snippet</div>snippet <span>tail</span></div>'
        b'<template><a class="result__a" href="https://example.org/template">secret template</a></template>'
        b"</body></html>"
    )
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] and result["errors"] == [] and len(result["results"]) == 1
    row = result["results"][0]
    assert (row["url"], row["title"], row["snippet"]) == (
        "https://example.org/manual",
        "Public title",
        "Public snippet tail",
    )
    assert "secret" not in json.dumps(result)


def test_ddg_visibility_is_independent_of_document_text_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from autostop_manager import j1_vin_documents as documents

    monkeypatch.setattr(documents, "MAX_TEXT_BYTES", 1)
    fake_transport(
        monkeypatch, [Response(body=b'<a class="result__a" href="https://example.org/manual">Public title</a>')]
    )
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] and result["results"][0]["title"] == "Public title"


def test_ddg_real_nesting_limit_is_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'<a class="result__a" href="https://example.org/manual">Manual</a>' + b"<div>" * 600
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert not result["ok"] and result["results"] == []
    assert result["errors"] == [{"provider": "duckduckgo", "error": "parse_failed"}]


def test_ddg_explicit_no_results_is_successful_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '<html><body><div id="links" class="results">'
        '<div class="result results_links result--no-result"><div class="result__body">'
        '<div class="no-results__container"><span class="no-results">No results found for '
        f"<strong>{VIN}</strong></span></div></div></div></div></body></html>"
    ).encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert (result["ok"], result["result_class"], result["results"], result["errors"]) == (True, "empty", [], [])
    assert result["providers"] == [{"provider": "duckduckgo", "outcome": "empty"}]


def test_ddg_recognized_results_preserve_links_and_snippets(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        '<html><body><div class="results"><div class="result results_links">'
        f'<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Freport">{VIN}</a>'
        '<a class="result__snippet" href="https://example.org/report">Public report snippet</a>'
        "</div></div></body></html>"
    ).encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] and result["result_class"] == "results" and result["errors"] == []
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert (row["url"], row["title"], row["snippet"]) == ("https://example.org/report", VIN, "Public report snippet")
    assert result["providers"] == [{"provider": "duckduckgo", "outcome": "results"}]


def test_ddg_recognition_is_independent_of_result_privacy_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    body = f'<a class="result__a" href="https://example.org/{OTHER}">Foreign VIN report</a>'.encode()
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert (result["ok"], result["result_class"], result["results"], result["errors"]) == (True, "empty", [], [])
    assert OTHER not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        b'<html><div class="anomaly-modal__modal">Bot challenge</div></html>',
        b'<html><form id = "challenge-form">Choose images</form></html>',
        b"<html><form ID='anomaly-form'>Verify you are human</form></html>",
    ],
)
def test_ddg_known_captcha_is_requires_human(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    fake_transport(monkeypatch, [Response(body=body)])
    result = network.search_vin(VIN, scope("duckduckgo"))
    assert result["ok"] is False and result["result_class"] == "error"
    assert result["errors"] == [{"provider": "duckduckgo", "error": "requires_human"}]


def test_expired_or_unsafe_search_has_no_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object) -> Any:
        raise AssertionError("provider_called")

    monkeypatch.setattr(network, "_searxng_search", forbidden)
    monkeypatch.setattr(network, "_duckduckgo_search", forbidden)
    assert network.search_vin(OTHER, scope())["errors"][0]["error"] == "unsafe_query"
    assert network.search_vin(VIN, scope(lifetime=-1))["errors"][0]["error"] == "scope_expired"
