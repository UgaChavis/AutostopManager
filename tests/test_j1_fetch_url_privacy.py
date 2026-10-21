"""Focused URL-context privacy checks for the J1 public fetcher."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from autostop_manager import j1_fetch


@pytest.mark.parametrize("segment", ["ab", "-", "._-"])
def test_segmented_non_vin_path_bounds_candidate_scans(monkeypatch: pytest.MonkeyPatch, segment: str) -> None:
    pattern = j1_fetch._VIN_TOKEN
    calls = 0

    class CountedPattern:
        def fullmatch(self, value: str):
            nonlocal calls
            calls += 1
            return pattern.fullmatch(value)

    monkeypatch.setattr(j1_fetch, "_VIN_TOKEN", CountedPattern())
    segments = 120
    assert not j1_fetch._url_component_contains_sensitive("/".join([segment] * segments))
    # Two characters per segment cannot form a 17-character VIN; separator-only
    # starts cannot add identifier characters and must not restore quadratic work.
    assert calls <= 9 * segments


def test_segment_scan_keeps_document_suffixes_and_later_sensitive_fields() -> None:
    vin = "ZZZ00000000000000"
    for width in (1, 3, 8):
        path = "/".join(vin[index : index + width] for index in range(0, len(vin), width))
        for suffix in ("", ".pdf", ".xhtml"):
            assert j1_fetch.public_url("https://example.org/" + path.lower() + suffix) == ""
            assert j1_fetch.public_url("https://example.org/?id=" + quote(path + suffix, safe="")) == ""
    prefix = "/".join(["ab"] * 30)
    assert j1_fetch.public_url("https://example.org/" + prefix + "/owner%40example.org") == ""
    assert j1_fetch.public_url("https://example.org/" + prefix + "?token=abcdefghijklmnop") == ""


@pytest.mark.parametrize("key", ["api_key", "token", "password", "secret"])
@pytest.mark.parametrize("representation", ["json", "mapping"])
def test_quoted_secret_keys_are_rejected_and_redacted(key: str, representation: str) -> None:
    credential = "synthetic-sentinel-credential"
    payload = json.dumps({key: credential}) if representation == "json" else str({key: credential})

    assert j1_fetch.contains_sensitive(payload)
    assert credential not in j1_fetch.redact_sensitive(payload)
    encoded = quote(payload, safe="")
    assert j1_fetch.public_url("https://example.org/report?context=" + encoded) == ""
    assert j1_fetch.public_url("https://example.org/report?context=" + quote(encoded, safe="")) == ""


def test_public_bulletin_pdf_is_allowed_and_not_redacted() -> None:
    bulletin = "https://static.nhtsa.gov/odi/tsbs/2014/SB-10063500-2280.pdf"

    assert j1_fetch.public_url(bulletin) == bulletin
    assert "SB-10063500-2280.pdf" in j1_fetch.redact_sensitive(f"Official bulletin: {bulletin}")

    vin = "ZZZ00000000000000"
    assert j1_fetch.contains_sensitive(f"Inspect {vin}")
    assert "[redacted]" in j1_fetch.redact_sensitive(f"Inspect {vin}")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/ZZZ00000000000000",
        "https://example.org/%5A%5A%5A00000000000000",
        "https://example.org/%255A%255A%255A00000000000000",
        "https://example.org/%25255A%25255A%25255A00000000000000",
        "https://example.org/ZZZ00000000000000/next",
        "https://example.org/%255A%255A%255A00000000000000/next",
        "https://example.org/ZZZ%2F000%2F000%2F000%2F00000",
        "https://ZZZ00000000000000.example.com/",
        "https://example.org/?vin=ZZZ00000000000000",
        "https://example.org/?vin=%255A%255A%255A00000000000000",
        "https://example.org/#vin=ZZZ00000000000000",
    ],
)
def test_public_url_rejects_vin_in_decoded_components(url: str) -> None:
    assert j1_fetch.public_url(url) == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/12345678901234567",
        "https://example.org/%31%32%33%34%35%36%37%38%39%30%31%32%33%34%35%36%37",
    ],
)
def test_public_url_rejects_direct_numeric_17_character_path_segment(url: str) -> None:
    assert j1_fetch.public_url(url) == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://owner@example.org/report",
        "https://user:password123@example.org/report",
        "https://example.org/report/owner%40example.org",
        "https://example.org/?token%3Dabcdefghijklmnop",
        "https://127.0.0.1/report",
    ],
)
def test_public_url_rejects_contacts_secrets_credentials_and_private_hosts(url: str) -> None:
    assert j1_fetch.public_url(url) == ""


def test_public_request_rejects_vin_redirect_before_next_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class RedirectResponse:
        status = 302

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Location", "/%25255A%25255A%25255A00000000000000")]

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        def __init__(self, *_args: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> RedirectResponse:
            return RedirectResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")
    monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", FakeConnection)

    with pytest.raises(ValueError, match="unsafe_redirect"):
        j1_fetch._request_public("https://example.org/report", max_bytes=100)
