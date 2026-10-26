"""Intrinsic HTML template visibility at the static J1 extraction boundary."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from autostop_manager import j1_fetch

_URL = "https://example.org/cooling-reference"
_TITLE = "Cooling reference"
_PUBLIC = (
    "Public cooling reference. Check the fan fuse and measure supply under load. "
    "Compare commanded fan state with measured operation. "
    "This page is available without authentication."
)
_VISIBLE = f"<p>{_PUBLIC}</p>"
_TAIL = "<p>Visible tail.</p>"
_EXPECTED = f"{_TITLE}\n{_PUBLIC}\nVisible tail."
_LOGIN = "<p>Sign in to continue</p>"


def _document(inner: str, *, title: str = _TITLE, encoding: str = "utf-8") -> bytes:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{inner}</body></html>".encode(encoding)


@pytest.fixture
def memory_fetch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bytes, dict[str, str]], dict]:
    responses = []
    calls = []

    def robots(url: str) -> tuple[bool, float]:
        assert url == _URL
        calls.append("robots")
        return True, 0.0

    def rate(origin: str, delay: float) -> None:
        assert (origin, delay) == ("https://example.org", 0.0)
        calls.append("rate")

    def request(url: str, **kwargs: object) -> tuple[int, dict[str, str], bytes, str]:
        assert url == _URL
        assert kwargs == {"max_bytes": j1_fetch.MAX_PDF_BYTES, "check_redirect_robots": True}
        calls.append("request")
        headers, body = responses.pop(0)
        return 200, headers, body, url

    def unused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Static HTML fixture reached an unused browser/PDF/transport adapter")

    monkeypatch.setattr(j1_fetch, "_robots_policy", robots)
    monkeypatch.setattr(j1_fetch, "_rate_limit", rate)
    monkeypatch.setattr(j1_fetch, "_request_public", request)
    for name in ("_browser_fallback", "_extract_pdf", "_PinnedHTTP", "_PinnedHTTPS", "_public_address"):
        monkeypatch.setattr(j1_fetch, name, unused)

    def fetch(body: bytes, headers: dict[str, str]) -> dict:
        calls.clear()
        responses.append((headers.copy(), body))
        result = j1_fetch.fetch_document(_URL)
        assert calls == ["robots", "rate", "request"]
        assert responses == []
        assert result.get("browser_attempted") is False
        assert result["extraction_method"] == "html_text"
        return result

    return fetch


def _extract(body: bytes, entrypoint: str, memory_fetch: Callable, headers: dict[str, str] | None = None) -> dict:
    headers = headers if headers is not None else {"content-type": "text/html; charset=utf-8"}
    if entrypoint == "direct":
        title, text = j1_fetch._html_to_text(body, headers)
        return {"title": title, "text": text}
    return memory_fetch(body, headers)


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(f"<template>{_LOGIN}</template>", id="ordinary"),
        pytest.param(f"<template shadowrootmode>{_LOGIN}</template>", id="boolean-mode"),
        pytest.param(f'<template shadowrootmode="">{_LOGIN}</template>', id="empty-mode"),
        pytest.param(f'<template shadowrootmode="none">{_LOGIN}</template>', id="invalid-mode"),
        pytest.param(f'<template shadowrootmode=" open ">{_LOGIN}</template>', id="mode-whitespace"),
        pytest.param(f'<template shadowrootmode="cloſed">{_LOGIN}</template>', id="non-ascii-mode"),
        pytest.param(f'<template shadowroot="open">{_LOGIN}</template>', id="obsolete-attribute"),
        pytest.param(f"<TEMPLATE><template>{_LOGIN}</template>{_LOGIN}</TEMPLATE>", id="nested-ordinary"),
        pytest.param(
            f'<template><div><template shadowrootmode="open">{_LOGIN}</template></div>{_LOGIN}</template>',
            id="shadow-within-ordinary",
        ),
        pytest.param(
            f'<template><div><template shadowrootmode="closed">{_LOGIN}</template></div></template>',
            id="closed-shadow-within-ordinary",
        ),
        pytest.param(
            '<template><script type="application/json">{"prompt":"Sign in to continue"}</script>'
            "<style>.dialog {display: block}</style><nav>Sample navigation</nav></template>",
            id="hidden-elements-within-ordinary",
        ),
        pytest.param(
            f"<template><title>Sample title</title><a href='/dialog'>{_LOGIN}</a></template>",
            id="template-title-is-not-document-title",
        ),
        pytest.param(f'<template shadowrootmode="invalid" shadowrootmode="open">{_LOGIN}</template>', id="first-mode"),
        pytest.param(f"<template/>{_LOGIN}</template>", id="nonvoid-slash"),
        pytest.param(f"<template><template/>{_LOGIN}</template>{_LOGIN}</template>", id="nested-nonvoid-slash"),
        pytest.param(f"<template><nav>{_LOGIN}</template>", id="inner-unclosed-nav"),
        pytest.param("<template><p>Unrendered sample details.</p></template>", id="nonchallenge-sample"),
    ],
)
def test_inert_template_does_not_supply_document_text_or_access_challenge(
    entrypoint: str, fragment: str, memory_fetch: Callable
) -> None:
    body = _document(f"<div>{fragment}</div>{_VISIBLE}{_TAIL}")
    result = _extract(body, entrypoint, memory_fetch)
    assert result.get("ok", True) is True
    assert (result["title"], result["text"]) == (_TITLE, _EXPECTED)


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize(
    "fragment",
    ["<template>", "<template/>", '<template><template shadowrootmode="open">'],
)
def test_unterminated_inert_template_stays_inert_until_eof(
    entrypoint: str, fragment: str, memory_fetch: Callable
) -> None:
    result = _extract(_document(f"{_VISIBLE}{fragment}{_LOGIN}{_TAIL}"), entrypoint, memory_fetch)
    assert result.get("ok", True) is True
    assert (result["title"], result["text"]) == (_TITLE, f"{_TITLE}\n{_PUBLIC}")


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize(
    "opening",
    [
        '<template shadowrootmode="open">',
        '<template shadowrootmode="closed">',
        '<TEMPLATE SHADOWROOTMODE="OPEN">',
        '<template shadowrootmode="ClOsEd">',
        "<template shadowrootmode=open>",
        '<template shadowrootmode="&#111;pen">',
        '<template shadowrootmode="open" shadowrootmode="invalid">',
        '<template shadowrootmode="open"/>',
    ],
)
@pytest.mark.parametrize("challenge", [False, True])
def test_declarative_shadow_content_and_visible_login_remain_visible(
    entrypoint: str, opening: str, challenge: bool, memory_fetch: Callable
) -> None:
    shadow_text = "Sign in to continue" if challenge else "Shadow public context."
    body = _document(f"<div>{opening}<p>{shadow_text}</p></template></div>{_VISIBLE}{_TAIL}")
    result = _extract(body, entrypoint, memory_fetch)
    if entrypoint == "public" and challenge:
        assert result["ok"] is False and result["error"] == "requires_human"
    else:
        assert result.get("ok", True) is True
        assert result["title"] == _TITLE
        assert result["text"] == f"{_TITLE}\n{shadow_text}\n{_PUBLIC}\nVisible tail."


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize("mode", ["open", "closed"])
def test_inert_child_of_shadow_preserves_shadow_siblings(entrypoint: str, mode: str, memory_fetch: Callable) -> None:
    body = _document(
        f'{_VISIBLE}<div><template shadowrootmode="{mode}"><p>Shadow prefix.</p>'
        f"<template><template>{_LOGIN}</template>{_LOGIN}</template><p>Shadow suffix.</p></template></div>{_TAIL}"
    )
    result = _extract(body, entrypoint, memory_fetch)
    assert result.get("ok", True) is True
    assert result["text"] == f"{_TITLE}\n{_PUBLIC}\nShadow prefix.\nShadow suffix.\nVisible tail."


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize("fragment", ["<template></template>", "</template>", "<script>Sign in to continue</script>"])
def test_template_controls_preserve_public_text(entrypoint: str, fragment: str, memory_fetch: Callable) -> None:
    result = _extract(_document(f"{fragment}{_VISIBLE}{_TAIL}"), entrypoint, memory_fetch)
    assert result.get("ok", True) is True
    assert (result["title"], result["text"]) == (_TITLE, _EXPECTED)


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
def test_template_contents_cannot_close_an_outer_hidden_element(entrypoint: str, memory_fetch: Callable) -> None:
    body = _document(f"{_VISIBLE}<nav><template></nav>{_LOGIN}</template>{_LOGIN}</nav>{_TAIL}")
    result = _extract(body, entrypoint, memory_fetch)
    assert result.get("ok", True) is True
    assert result["text"] == _EXPECTED


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize(
    ("content_type", "encoding"),
    [
        ("text/html", "utf-8"),
        ("text/html; charset=unknown-codec", "utf-8"),
        ("text/html; charset=windows-1251", "cp1251"),
    ],
)
def test_template_filter_preserves_decoder_dlp_and_inputs(
    entrypoint: str, content_type: str, encoding: str, memory_fetch: Callable
) -> None:
    visible = f"{_PUBLIC} Кириллица и контакт staff@example.org, для проверки."
    body = _document(f"<template>{_LOGIN}</template><p>{visible}</p>", encoding=encoding)
    headers = {"content-type": content_type}
    original = body, headers.copy()
    result = _extract(body, entrypoint, memory_fetch, headers)
    assert result.get("ok", True) is True
    assert "Кириллица" in result["text"] and "\ufffd" not in result["text"]
    assert "staff@example.org" not in result["text"] and "[redacted]," in result["text"]
    assert (body, headers) == original


@pytest.mark.parametrize("entrypoint", ["direct", "public"])
@pytest.mark.parametrize("fragment", ["<template/>", '<template shadowrootmode="open"/>'])
@pytest.mark.parametrize(
    "content_type", ["application/xhtml+xml", "Application/XHTML+XML", "application/xhtml+xml; charset=utf-8"]
)
def test_xhtml_self_closing_template_keeps_visible_siblings(
    entrypoint: str, fragment: str, content_type: str, memory_fetch: Callable
) -> None:
    body = _document(f"{fragment}{_VISIBLE}{_TAIL}")
    result = _extract(body, entrypoint, memory_fetch, {"content-type": content_type})
    assert result.get("ok", True) is True
    assert (result["title"], result["text"]) == (_TITLE, _EXPECTED)


@pytest.mark.parametrize(
    "fragment",
    [
        f"<template>{_VISIBLE}</template>",
        f'<template><div><template shadowrootmode="open">{_VISIBLE}</template></div></template>',
    ],
)
def test_inert_only_document_cannot_become_a_successful_source(fragment: str, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _document(fragment, title="")
    headers = {"content-type": "text/html; charset=utf-8"}
    assert j1_fetch._html_to_text(body, headers) == ("", "")
    requests = []
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)

    def request(url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes, str]:
        requests.append(url)
        return 200, headers, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    result = j1_fetch.fetch_document(_URL, allow_browser=False)
    assert result == {
        "ok": False,
        "error": "browser_limit_reached",
        "extraction_method": "html_text",
        "browser_attempted": False,
    }
    assert requests == [_URL]
