"""Plain documents and HTML extraction honor actual HTTP charset parameters."""

from __future__ import annotations

import socket
import sqlite3
import subprocess

import pytest

from autostop_manager import j1_fetch, j1_vin_documents as documents


TITLE = "Регламент обслуживания"
TEXT = "Масляный фильтр заменяется при техническом обслуживании автомобиля. " * 3
URL = "https://example.org/synthetic-maintenance.txt"
CHARSET_CASES = [
    pytest.param("charset=windows-1251", "cp1251", id="legacy"),
    pytest.param('CHARSET = "Windows-1251"', "cp1251", id="quoted-case-space"),
    pytest.param("charset=koi8-r", "koi8-r", id="koi8-r"),
    pytest.param("charset=utf-16", "utf-16", id="utf16"),
    pytest.param('note="charset=utf-8"; charset=windows-1251', "cp1251", id="quoted-decoy-first"),
    pytest.param('note="charset=windows-1251; ignored=x"', "utf-8", id="quoted-decoy-without-charset"),
    pytest.param("xcharset=utf-8; charset=windows-1251", "cp1251", id="other-parameter-name"),
    pytest.param(r'charset="windows\-1251"', "cp1251", id="quoted-pair"),
    pytest.param("", "utf-8", id="default"),
    pytest.param("charset=utf-8", "utf-8", id="utf8"),
    pytest.param("charset=missing-synthetic-codec", "utf-8", id="unknown-fallback"),
]


def denied(*_args, **_kwargs):
    raise AssertionError("document charset tests must not access external state or launch subprocesses")


@pytest.fixture(autouse=True)
def forbid_external_state(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    for name in ("_request_public", "_robots_policy", "_rate_limit", "_browser_fallback", "_extract_pdf"):
        monkeypatch.setattr(j1_fetch, name, denied)


@pytest.mark.parametrize("parameters,encoding", CHARSET_CASES)
def test_plain_fetch_decodes_declared_charset_without_changing_result_contract(monkeypatch, parameters, encoding):
    content_type = "text/plain" + ("; " + parameters if parameters else "")
    body = TEXT.encode(encoding)
    requests = []

    def request(url, **_kwargs):
        requests.append(url)
        return 200, {"content-type": content_type}, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)

    result = j1_fetch.fetch_document(URL, allow_browser=False)

    assert result == {
        "ok": True,
        "url": URL,
        "title": "",
        "text": TEXT,
        "kind": "text",
        "extraction_method": "plain_text",
        "browser_attempted": False,
    }
    assert requests == [URL]


@pytest.mark.parametrize("parameters,encoding", CHARSET_CASES)
def test_page_extraction_uses_actual_charset_and_leaves_no_files(tmp_path, parameters, encoding):
    content_type = "text/html" + ("; " + parameters if parameters else "")
    body = f"<html><head><title>{TITLE}</title></head><body><p>{TEXT}</p></body></html>".encode(encoding)

    result = documents.extract_content(body, content_type, "https://example.org/manual", tmp_path)

    assert result["ok"] and result["title"] == TITLE
    assert result["pages"] == [{"page": 1, "text": TEXT.strip()}]
    assert result["kind"] == "html" and result["extraction_method"] == "html_text"
    assert not result["truncated"] and result["limitations"] == ["static_html_visibility"]
    assert list(tmp_path.iterdir()) == []


def test_decoding_preserves_each_callers_redaction_contract(tmp_path):
    text = TEXT + "Contact reader@example.invalid."
    plain = j1_fetch._extract_static_content(
        "text/plain; charset=windows-1251",
        text.encode("cp1251"),
        URL,
        {"content-type": "text/plain; charset=windows-1251"},
    )
    html = documents.extract_content(
        f"<p>{text}</p>".encode("cp1251"), "text/html; charset=windows-1251", "https://example.org/manual", tmp_path
    )

    assert TEXT in plain["text"] and "reader@example.invalid" not in plain["text"]
    assert "[redacted]" in plain["text"]
    assert html["pages"][0]["text"] == text
    assert list(tmp_path.iterdir()) == []


def test_body_limits_and_depleted_budget_still_precede_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(j1_fetch, "MAX_HTML_BYTES", 10)
    monkeypatch.setattr(documents, "MAX_HTML_BYTES", 10)
    body = "Русский текст".encode("utf-16")
    assert len(body) > 10

    plain = j1_fetch._extract_static_content("text/plain; charset=utf-16", body, URL, {})
    html = documents.extract_content(body, "text/html; charset=utf-16", "", tmp_path)
    expired = documents.extract_content(b"<p>x</p>", "text/html", "", tmp_path, timeout_seconds=0)

    assert plain == {"ok": False, "error": "document_too_large"}
    assert html["error"] == "document_body_too_large"
    assert expired["error"] == "document_timeout"
    assert list(tmp_path.iterdir()) == []


def test_decoded_text_caps_and_unknown_charset_replacement_are_preserved(tmp_path, monkeypatch):
    text = "А " * j1_fetch.MAX_TEXT_CHARS
    plain = j1_fetch._extract_static_content(
        "text/plain; charset=windows-1251",
        text.encode("cp1251"),
        URL,
        {"content-type": "text/plain; charset=windows-1251"},
    )
    assert plain["text"] == text[: j1_fetch.MAX_TEXT_CHARS]
    monkeypatch.setattr(documents, "MAX_TEXT_BYTES", 32)
    html = documents.extract_content(f"<p>{text}</p>".encode("cp1251"), "text/html; charset=windows-1251", "", tmp_path)
    assert html["truncated"] and "text_limit" in html["limitations"]
    assert len(html["pages"][0]["text"].encode("utf-8")) <= 32
    fallback = j1_fetch._extract_static_content(
        "text/plain; charset=missing-synthetic-codec",
        b"public \xff text",
        URL,
        {"content-type": "text/plain; charset=missing-synthetic-codec"},
    )
    assert fallback["text"] == "public \ufffd text"
    assert list(tmp_path.iterdir()) == []
