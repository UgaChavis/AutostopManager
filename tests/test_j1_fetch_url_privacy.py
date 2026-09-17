"""Focused URL-context privacy checks for the J1 public fetcher."""

from __future__ import annotations

import pytest

from autostop_manager import j1_fetch


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
