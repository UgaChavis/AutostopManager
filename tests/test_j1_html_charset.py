"""Equivalent HTTP charset parameters must yield the same public HTML text."""

import socket
import sqlite3
import subprocess

import pytest

from autostop_manager import config, j1_fetch


TITLE = "Замена масляного фильтра"
PARAGRAPH = "Регламент обслуживания: масляный фильтр заменяется при техническом обслуживании. " * 3
URL = "https://example.org/synthetic-maintenance.html"
CHARSET_CASES = [
    pytest.param("text/html; charset=windows-1251", "cp1251", id="legacy-unquoted"),
    pytest.param('text/html; charset="windows-1251"', "cp1251", id="legacy-quoted"),
    pytest.param('text/html; CHARSET="Windows-1251"', "cp1251", id="quoted-parameter-case"),
    pytest.param('text/html; charset = "windows-1251"', "cp1251", id="quoted-parameter-whitespace"),
    pytest.param("text/html; charset = windows-1251", "cp1251", id="unquoted-parameter-whitespace"),
    pytest.param("text/html; CHARSET=WINDOWS-1251", "cp1251", id="unquoted-parameter-case"),
    pytest.param("text/html; charset=utf-8", "utf-8", id="utf8-unquoted"),
    pytest.param('text/html; charset="utf-8"', "utf-8", id="utf8-quoted"),
    pytest.param("text/html", "utf-8", id="default-utf8"),
    pytest.param("text/html; charset=missing-synthetic-codec", "utf-8", id="unknown-codec-fallback"),
    pytest.param('text/html; charset="missing-synthetic-codec"', "utf-8", id="quoted-unknown-codec-fallback"),
    pytest.param('text/html; note="charset=windows-1251"; charset=utf-8', "utf-8", id="quoted-other-parameter"),
    pytest.param('text/html; note="charset=utf-8"; charset=windows-1251', "cp1251", id="real-legacy-after-note"),
    pytest.param('text/html; note="charset=windows-1251; ignored=x"', "utf-8", id="quoted-note-without-charset"),
    pytest.param("text/html; xcharset=windows-1251; charset=utf-8", "utf-8", id="different-parameter-name"),
    pytest.param('text/html; charset=windows-1251; note="charset=utf-8"', "cp1251", id="real-legacy-before-note"),
    pytest.param(r'text/html; charset="windows-12\51"', "cp1251", id="quoted-pair-digit"),
    pytest.param(r'text/html; charset="win\dows-1251"', "cp1251", id="quoted-pair-letter"),
    pytest.param(r'text/html; charset="windows\-1251"', "cp1251", id="quoted-pair-hyphen"),
    pytest.param(r'text/html; charset="windows-12\\51"', "utf-8", id="literal-backslash-unknown-codec"),
    pytest.param(
        r'text/html; note="charset=windows-1251\";still-note"; charset=utf-8',
        "utf-8",
        id="escaped-quote-and-semicolon-in-note",
    ),
    pytest.param('text/html;\r\n charset="windows-1251"', "cp1251", id="folded-header-value"),
]


def _denied(*args, **kwargs):
    raise AssertionError("charset extraction must not access external or default runtime state")


@pytest.fixture(autouse=True)
def forbid_external_state(monkeypatch):
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)
    monkeypatch.setattr(socket, "getaddrinfo", _denied)
    monkeypatch.setattr(sqlite3, "connect", _denied)
    monkeypatch.setattr(subprocess, "Popen", _denied)
    monkeypatch.setattr(config, "get_db_path", _denied)
    for name in (
        "_PinnedHTTP",
        "_PinnedHTTPS",
        "_request_public",
        "_robots_policy",
        "_rate_limit",
        "_browser_fallback",
        "_pdf_to_text",
    ):
        monkeypatch.setattr(j1_fetch, name, _denied)


def _body(encoding):
    return f"<html><head><title>{TITLE}</title></head><body><p>{PARAGRAPH}</p></body></html>".encode(encoding)


def _assert_decoded(title, text):
    assert title == TITLE
    assert PARAGRAPH.strip() in text
    assert "\ufffd" not in text


@pytest.mark.parametrize("content_type,encoding", CHARSET_CASES)
def test_html_text_honors_the_actual_charset_parameter(content_type, encoding):
    title, text = j1_fetch._html_to_text(_body(encoding), {"content-type": content_type})

    _assert_decoded(title, text)


@pytest.mark.parametrize("content_type,encoding", CHARSET_CASES)
def test_public_fetch_document_preserves_charset_decoded_title_and_text(monkeypatch, content_type, encoding):
    requests = []

    def fake_request(url, **kwargs):
        requests.append(url)
        return 200, {"content-type": content_type}, _body(encoding), url

    monkeypatch.setattr(j1_fetch, "_request_public", fake_request)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    result = j1_fetch.fetch_document(URL, allow_browser=False)

    assert result["ok"] is True
    assert result["url"] == URL
    assert result["kind"] == "html"
    assert result["extraction_method"] == "html_text"
    assert result["browser_attempted"] is False
    _assert_decoded(result["title"], result["text"])
    assert requests == [URL]
