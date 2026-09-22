"""Bounded public-web retrieval for J1.

Every public connection uses a checked DNS answer as its socket destination.
The HTTP Host and TLS server name remain the original hostname. Redirects are
resolved and checked again; proxies, cookies and authenticated pages are not used.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
import os
import pwd
import re
import resource
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote_plus, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .j1_sources import classify_source, discovery_domain

USER_AGENT = "AutoStop-J1/1.0 (+public research; respects robots.txt)"
MAX_HTML_BYTES = 2_000_000
MAX_PDF_BYTES = 8_000_000
MAX_TEXT_CHARS = 50_000
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_RESULTS_PER_DOMAIN = 3
MAX_SEARCH_SNIPPET_CHARS = 600
MAX_DOCUMENT_RETRIES = 1
_RETRY_DELAY_SECONDS = 0.25
_VIN = re.compile(
    r"(?<![A-HJ-NPR-Z0-9])(?:[A-HJ-NPR-Z0-9][ ._/\\-]?){16}[A-HJ-NPR-Z0-9](?![A-HJ-NPR-Z0-9])",
    re.I,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Z]{2,}(?![\w.-])", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+7|8|7(?=9\d{2}))[\s().-]*\d{3}(?:[\s().-]*\d){7}(?!\d)")
_SECRET = re.compile(r"(?i)(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s&]{8,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b")
_API_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")
_EMBEDDED_URL = re.compile(r"(?i)(?<!\w)[a-z][a-z0-9+.-]*://[^\s<>]+")
_VIN_SEPARATORS = re.compile(r"[ ._/\\-]+")
_DOCUMENT_SUFFIX = re.compile(r"\.(?:pdf|html?|xhtml|txt|xml|json|csv|docx?|xlsx?|pptx?)$", re.I)
_URL_FIELD_SEPARATOR = re.compile(r"[?&;=#]+")
_VIN_TOKEN = re.compile(r"^[A-HJ-NPR-Z0-9 ._/\\-]+$", re.I)
_ROBOTS: dict[str, tuple[float, RobotFileParser | None, bool, float]] = {}
_ROBOTS_LOCK = threading.Lock()
_HOST_NEXT: dict[str, float] = {}
_RATE_LOCK = threading.Lock()
_RETRYABLE_DOCUMENT_ERRORS = frozenset(
    {
        "fetch_failed",
        "rate_limited",
        "http_server_error",
        "ocr_unavailable",
        "ocr_timeout",
        "ocr_render_failed",
        "ocr_extract_failed",
        "browser_unavailable",
        "browser_timeout",
        "browser_render_failed",
        "browser_busy",
    }
)


def contains_sensitive(value: str) -> bool:
    """Reject sensitive user input before sending or persisting it."""

    decoded = _decode_percent_layers(value)
    return bool(
        any(_looks_like_vin(match.group()) for match in _VIN.finditer(decoded))
        or any(pattern.search(decoded) for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET))
    )


def _decode_percent_layers(value: str) -> str:
    # URL inputs are bounded to 2048 bytes before this helper is reached.  A
    # fixed, generously high iteration cap catches nested percent-encoding of
    # VINs and secrets without permitting an attacker to turn decoding itself
    # into an unbounded operation.
    decoded = str(value or "")
    for _ in range(64):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _looks_like_vin(candidate: str) -> bool:
    # Ordinary prose can contain 17 permitted letters across spaces or dashes.
    # Split VINs have a numeric serial; an uninterrupted 17-character token is
    # treated conservatively as a VIN even when it has no digits.
    if len(candidate) == 17:
        return True
    return sum(char.isdigit() for char in candidate) >= 5


def _is_full_url_vin(candidate: str) -> bool:
    """Recognize an actual 17-character VIN token without crossing a file suffix.

    ``_VIN`` intentionally has broad matching for ordinary free text.  Applied
    to a URL or retrieved document verbatim it can consume the ``.pdf`` suffix
    of a bulletin ID (for example ``SB-10063500-2280.pdf``) as three VIN
    characters.  A URL component has structure, so remove a known document
    suffix before counting the identifier itself.
    """

    identifier = _DOCUMENT_SUFFIX.sub("", candidate)
    return len(_VIN_SEPARATORS.sub("", identifier)) == 17


def _url_component_contains_sensitive(value: str) -> bool:
    """Check decoded URL data without applying prose-only VIN heuristics."""

    decoded = _decode_percent_layers(value)
    fields = _URL_FIELD_SEPARATOR.split(decoded)

    def has_vin(field: str) -> bool:
        segments = [segment for segment in field.split("/") if segment]
        if any(_is_full_url_vin(match.group()) for segment in segments for match in _VIN.finditer(segment)):
            return True
        # An encoded slash can make a deliberately separated VIN look like a
        # path.  Join only whole adjacent path segments; this avoids a regex
        # accidentally treating a following URL segment as a document suffix.
        for start in range(len(segments)):
            candidate = ""
            for end in range(start, len(segments)):
                candidate = segments[end] if not candidate else candidate + "/" + segments[end]
                if _VIN_TOKEN.fullmatch(candidate) and _is_full_url_vin(candidate):
                    return True
        return False

    return bool(
        any(has_vin(field) for field in fields)
        or any(pattern.search(decoded) for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET))
    )


def _hostname_contains_sensitive(host: str) -> bool:
    """Reject a full VIN used as an individual public DNS label."""

    return any(_VIN_TOKEN.fullmatch(label) and _is_full_url_vin(label) for label in host.casefold().split("."))


def redact_sensitive(value: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "")[: max(0, limit * 2)]
    text = _VIN.sub(
        lambda match: (
            "[redacted]" if _looks_like_vin(match.group()) and _is_full_url_vin(match.group()) else match.group()
        ),
        text,
    )
    for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET):
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def public_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or host.casefold() in {"localhost", "localhost.localdomain"}
        or host.casefold().endswith((".localhost", ".local", ".internal", ".onion"))
    ):
        return ""
    # User input is deliberately checked as prose by ``contains_sensitive``.
    # A direct URL is different: validate its separately decoded components so
    # a public document name is not mistaken for a separator-heavy VIN while
    # VINs, contacts and secrets embedded in data remain non-egressable.
    if _hostname_contains_sensitive(host) or any(
        _url_component_contains_sensitive(component) for component in (parsed.path, parsed.query, parsed.fragment)
    ):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def contains_unsafe_url(value: str) -> bool:
    """Reject private and non-web URLs even when embedded in a search phrase."""

    return any(not public_url(match.group().rstrip(".,;)]}")) for match in _EMBEDDED_URL.finditer(value))


def _public_address(host: str, port: int) -> str:
    """Reject an entire DNS answer if *any* address is non-public."""

    addresses = {str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("unsafe_dns_answer")
    return sorted(addresses)[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        self._j1_ssl_context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._j1_ssl_context)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._j1_ssl_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _request_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 10.0,
    check_redirect_robots: bool = False,
    read_body: bool = True,
) -> tuple[int, dict[str, str], bytes, str]:
    current = public_url(url)
    if not current:
        raise ValueError("unsafe_url")
    for _ in range(4):
        parsed = urlsplit(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = _public_address(host, port)
        connection: http.client.HTTPConnection = (
            _PinnedHTTPS(host, ip, port, timeout) if parsed.scheme == "https" else _PinnedHTTP(host, ip, port, timeout)
        )
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/pdf,text/plain",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            status = response.status
            headers = {name.casefold(): value for name, value in response.getheaders()}
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location", "")
                next_url = public_url(urljoin(current, location)) if location else ""
                if not next_url:
                    raise ValueError("unsafe_redirect")
                if check_redirect_robots:
                    allowed, delay = _robots_policy(next_url)
                    if not allowed:
                        raise ValueError("redirect_robots_disallowed")
                    next_parsed = urlsplit(next_url)
                    _rate_limit(f"{next_parsed.scheme}://{next_parsed.netloc}", delay)
                current = next_url
                continue
            size_header = headers.get("content-length", "")
            if size_header.isdigit() and int(size_header) > max_bytes:
                raise ValueError("document_too_large")
            if headers.get("content-encoding", "identity").casefold() != "identity":
                raise ValueError("unsupported_content_encoding")
            body = response.read(max_bytes + 1) if read_body else b""
            if len(body) > max_bytes:
                raise ValueError("document_too_large")
            return status, headers, body, current
        finally:
            connection.close()
    raise ValueError("too_many_redirects")


def _robots_policy(url: str) -> tuple[bool, float]:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with _ROBOTS_LOCK:
        cached = _ROBOTS.get(origin)
    if cached and cached[0] > time.monotonic():
        parser, available, delay = cached[1:]
        return (available and (parser is None or parser.can_fetch(USER_AGENT, url))), delay
    try:
        status, _, body, final_url = _request_public(origin + "/robots.txt", max_bytes=250_000, timeout=7)
        if urlsplit(final_url).netloc != parsed.netloc:
            raise ValueError("robots_redirected")
        if status == 404:
            policy: tuple[RobotFileParser | None, bool, float] = (None, True, 1.0)
        elif status == 200:
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", "replace").splitlines())
            crawl_delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 1.0
            policy = (parser, True, min(max(float(crawl_delay), 1.0), 30.0))
        else:
            policy = (None, False, 1.0)
    except (OSError, ValueError, TimeoutError, http.client.HTTPException):
        policy = (None, False, 1.0)
    with _ROBOTS_LOCK:
        _ROBOTS[origin] = (time.monotonic() + 3600, *policy)
    parser, available, delay = policy
    return (available and (parser is None or parser.can_fetch(USER_AGENT, url))), delay


def _rate_limit(origin: str, delay: float) -> None:
    with _RATE_LOCK:
        wait = max(0.0, _HOST_NEXT.get(origin, 0.0) - time.monotonic())
        _HOST_NEXT[origin] = time.monotonic() + wait + delay
    if wait:
        time.sleep(wait)


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[str] = []
        self._hidden = 0
        self._title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"}:
            self._hidden += 1
        if tag == "title":
            self._title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href and len(self.links) < 100:
                self.links.append(href)
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"} and self._hidden:
            self._hidden -= 1
        if tag == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title_parts.append(data)
        if not self._hidden:
            self.parts.append(data)


def _html_to_text(body: bytes, headers: dict[str, str]) -> tuple[str, str]:
    charset_match = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        decoded = body.decode(charset, "replace")
    except LookupError:
        decoded = body.decode("utf-8", "replace")
    parser = _PageText()
    parser.feed(decoded)
    title = " ".join(" ".join(parser.title_parts).split())[:200]
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n[\s\n]*\n", "\n", text).strip()
    return title, redact_sensitive(text)


def _pdf_to_text(body: bytes) -> str:
    def restrict() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            os.setgroups([])
            os.setgid(nobody.pw_gid)
            os.setuid(nobody.pw_uid)

    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["/usr/bin/pdftotext", "-f", "1", "-l", "100", "-layout", "-", "-"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                preexec_fn=restrict,
            )
            try:
                process.communicate(input=body, timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return ""
            if process.returncode != 0:
                return ""
            output.seek(0)
            extracted = output.read(MAX_TEXT_CHARS * 4)
    except (OSError, subprocess.SubprocessError, KeyError):
        return ""
    return redact_sensitive(extracted.decode("utf-8", "replace"))


def _extract_pdf(body: bytes) -> dict[str, Any]:
    text = _pdf_to_text(body)
    if text:
        return {"ok": True, "text": text, "extraction_method": "pdf_text"}
    # Kept lazy to avoid a module cycle: the OCR helper redacts with this
    # module's DLP function, but static fetch still works without its runtime.
    try:
        from .j1_ocr import extract_scanned_pdf

        ocr = extract_scanned_pdf(body, max_chars=MAX_TEXT_CHARS)
    except Exception:  # noqa: BLE001 - parser failure is a partial source failure.
        ocr = {"ok": False, "error": "ocr_unavailable"}
    if not isinstance(ocr, dict) or ocr.get("ok") is not True:
        error = ocr.get("error") if isinstance(ocr, dict) else ""
        return {"ok": False, "error": str(error or "pdf_extract_failed")[:80], "extraction_method": "pdf_ocr"}
    return {"ok": True, "text": redact_sensitive(str(ocr.get("text") or "")), "extraction_method": "pdf_ocr"}


def _extract_static_content(content_type: str, body: bytes, final_url: str, headers: dict[str, str]) -> dict[str, Any]:
    if "pdf" in content_type or final_url.casefold().endswith(".pdf"):
        extracted = _extract_pdf(body)
        if extracted.get("ok") is not True:
            return extracted
        return {
            "ok": True,
            "kind": "pdf",
            "title": final_url.rsplit("/", 1)[-1][:200],
            "text": extracted["text"],
            "extraction_method": extracted["extraction_method"],
        }
    if "html" in content_type or "text/plain" in content_type:
        if len(body) > MAX_HTML_BYTES:
            return {"ok": False, "error": "document_too_large"}
        if "html" in content_type:
            title, text = _html_to_text(body, headers)
            kind, method = "html", "html_text"
        else:
            title, text, kind, method = "", redact_sensitive(body.decode("utf-8", "replace")), "text", "plain_text"
        return {"ok": True, "kind": kind, "title": title, "text": text, "extraction_method": method}
    return {"ok": False, "error": "unsupported_media"}


def _browser_fallback(
    final_url: str,
    title: str,
    *,
    allow_browser: bool,
    max_chars: int = MAX_TEXT_CHARS,
) -> dict[str, Any]:
    if allow_browser is not True:
        return {"ok": False, "error": "browser_limit_reached"}
    try:
        from .j1_browser import isolation_verified, render_page
    except Exception:  # noqa: BLE001 - the optional client can be unavailable in a minimal release.
        return {"ok": False, "error": "browser_isolation_unverified"}
    try:
        verified = isolation_verified()
    except Exception:  # noqa: BLE001 - a malformed optional attestation cannot stop static J1.
        verified = False
    if not verified:
        return {"ok": False, "error": "browser_isolation_unverified"}
    # The browser makes a second request, so reuse the cached policy and host
    # delay before giving it the static response's safe final URL.
    allowed, delay = _robots_policy(final_url)
    if not allowed:
        return {"ok": False, "error": "robots_disallowed"}
    origin = urlsplit(final_url)
    _rate_limit(f"{origin.scheme}://{origin.netloc}", delay)
    bounded_chars = max(80, min(int(max_chars), MAX_TEXT_CHARS)) if type(max_chars) is int else MAX_TEXT_CHARS
    try:
        rendered = render_page(final_url, max_chars=bounded_chars, timeout_seconds=20)
    except Exception:  # noqa: BLE001 - isolated renderer is an optional partial source.
        rendered = {"ok": False, "error": "browser_unavailable"}
    if not isinstance(rendered, dict) or rendered.get("ok") is not True:
        error = rendered.get("error") if isinstance(rendered, dict) else ""
        return {"ok": False, "error": str(error or "browser_render_failed")[:80], "browser_attempted": True}
    rendered_url = public_url(str(rendered.get("url") or ""))
    rendered_text = redact_sensitive(str(rendered.get("text") or ""), limit=bounded_chars)
    if not rendered_url or len(rendered_text) < 80:
        return {"ok": False, "error": "browser_response_invalid", "browser_attempted": True}
    if any(
        marker in rendered_text[:3000].casefold()
        for marker in ("captcha", "введите капчу", "sign in to continue", "please log in")
    ):
        return {"ok": False, "error": "requires_human", "browser_attempted": True}
    return {
        "ok": True,
        "url": rendered_url,
        "title": redact_sensitive(str(rendered.get("title") or title), limit=200),
        "text": rendered_text,
        "extraction_method": "browser_dom",
        "browser_attempted": True,
    }


def _retry_after_seconds(headers: dict[str, str]) -> float:
    """Use a tiny bounded delay for one respectful 429 retry.

    Date-form Retry-After is deliberately ignored: parsing it would add clock
    policy to a worker whose retry is intentionally short and optional.
    """

    return _bounded_retry_delay(headers.get("retry-after", ""))


def _bounded_retry_delay(value: object) -> float:
    try:
        retry_after = float(str(value or ""))
    except (TypeError, ValueError):
        retry_after = _RETRY_DELAY_SECONDS
    if not math.isfinite(retry_after):
        retry_after = _RETRY_DELAY_SECONDS
    return min(3.0, max(_RETRY_DELAY_SECONDS, retry_after))


def _fetch_document_once(url: str, *, allow_browser: bool = True) -> dict[str, Any]:
    """Run one fail-closed document retrieval attempt."""

    safe_url = public_url(url)
    if not safe_url:
        return {"ok": False, "error": "unsafe_url"}
    allowed, delay = _robots_policy(safe_url)
    if not allowed:
        return {"ok": False, "error": "robots_disallowed"}
    parsed = urlsplit(safe_url)
    _rate_limit(f"{parsed.scheme}://{parsed.netloc}", delay)
    try:
        status, headers, body, final_url = _request_public(
            safe_url, max_bytes=MAX_PDF_BYTES, check_redirect_robots=True
        )
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
        return {"ok": False, "error": "fetch_failed"}
    except ValueError as exc:
        # Validation and robots/redirect guards are deterministic safety
        # failures.  They must never become a retry loop against a blocked or
        # private destination.
        code = str(exc)
        return {
            "ok": False,
            "error": code
            if code
            in {
                "unsafe_url",
                "unsafe_redirect",
                "unsafe_dns_answer",
                "redirect_robots_disallowed",
                "robots_redirected",
                "document_too_large",
                "unsupported_content_encoding",
                "too_many_redirects",
            }
            else "fetch_rejected",
        }
    if status in {401, 403, 429}:
        if status == 429:
            return {"ok": False, "error": "rate_limited", "retry_after_seconds": _retry_after_seconds(headers)}
        return {"ok": False, "error": "access_restricted"}
    if 500 <= status <= 599:
        return {"ok": False, "error": "http_server_error"}
    if status != 200:
        return {"ok": False, "error": "http_error"}
    extracted = _extract_static_content(headers.get("content-type", "").casefold(), body, final_url, headers)
    if extracted.get("ok") is not True:
        return extracted
    if len(extracted["text"]) < 80:
        if extracted["kind"] != "html":
            return {"ok": False, "error": "empty_or_dynamic", "extraction_method": extracted["extraction_method"]}
        fallback = _browser_fallback(final_url, extracted["title"], allow_browser=allow_browser)
        if fallback.get("ok") is not True:
            return {
                "ok": False,
                "error": fallback["error"],
                "extraction_method": "browser_dom"
                if fallback.get("browser_attempted")
                else extracted["extraction_method"],
                "browser_attempted": bool(fallback.get("browser_attempted")),
            }
        extracted.update(fallback)
        final_url = fallback["url"]
    text = extracted["text"]
    if any(
        marker in text[:3000].casefold()
        for marker in ("captcha", "введите капчу", "sign in to continue", "please log in")
    ):
        return {
            "ok": False,
            "error": "requires_human",
            "extraction_method": extracted["extraction_method"],
            "browser_attempted": bool(extracted.get("browser_attempted")),
        }
    return {
        "ok": True,
        "url": final_url,
        "title": redact_sensitive(extracted["title"], limit=200),
        "text": text[:MAX_TEXT_CHARS],
        "kind": extracted["kind"],
        "extraction_method": extracted["extraction_method"],
        "browser_attempted": bool(extracted.get("browser_attempted")),
    }


def fetch_document(url: str, *, allow_browser: bool = True) -> dict[str, Any]:
    """Return bounded public text with at most one transient-error retry.

    Robots, privacy, authentication, CAPTCHA and validation failures never
    retry.  The retry starts a new complete guarded request, so DNS and robots
    controls are re-applied before any second connection.
    """

    first = _fetch_document_once(url, allow_browser=allow_browser)
    if first.get("ok") is True or str(first.get("error") or "") not in _RETRYABLE_DOCUMENT_ERRORS:
        return first
    if first.get("error") == "rate_limited":
        time.sleep(_bounded_retry_delay(first.get("retry_after_seconds")))
    second = _fetch_document_once(url, allow_browser=allow_browser)
    # Retry timing is internal: preserve the original public result contract
    # and never surface a server-provided header through MCP.
    second.pop("retry_after_seconds", None)
    return second


def fetch_browser_document(url: str, *, max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    """Force an isolated browser render after a no-parser guarded preflight.

    The Manager process validates URL DLP, pinned DNS, robots, redirects,
    response status and media type, but deliberately does not read or parse
    the untrusted response body.  Only the isolated renderer handles page
    content; its bounded result is redacted again before it leaves J1.
    """

    safe_url = public_url(url)
    if not safe_url:
        return {"ok": False, "error": "unsafe_url"}
    try:
        from .j1_browser import isolation_verified

        verified = isolation_verified()
    except Exception:  # noqa: BLE001 - optional browser readiness fails closed.
        verified = False
    if not verified:
        return {"ok": False, "error": "browser_isolation_unverified"}
    bounded_chars = max(80, min(int(max_chars), MAX_TEXT_CHARS)) if type(max_chars) is int else MAX_TEXT_CHARS
    allowed, delay = _robots_policy(safe_url)
    if not allowed:
        return {"ok": False, "error": "robots_disallowed"}
    parsed = urlsplit(safe_url)
    _rate_limit(f"{parsed.scheme}://{parsed.netloc}", delay)
    try:
        status, headers, _body, final_url = _request_public(
            safe_url,
            max_bytes=MAX_HTML_BYTES,
            check_redirect_robots=True,
            read_body=False,
        )
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
        return {"ok": False, "error": "fetch_failed"}
    except ValueError as exc:
        code = str(exc)
        return {
            "ok": False,
            "error": code
            if code
            in {
                "unsafe_url",
                "unsafe_redirect",
                "unsafe_dns_answer",
                "redirect_robots_disallowed",
                "robots_redirected",
                "document_too_large",
                "unsupported_content_encoding",
                "too_many_redirects",
            }
            else "fetch_rejected",
        }
    if status in {401, 403}:
        return {"ok": False, "error": "access_restricted"}
    if status == 429:
        return {"ok": False, "error": "rate_limited"}
    if 500 <= status <= 599:
        return {"ok": False, "error": "http_server_error"}
    if status != 200:
        return {"ok": False, "error": "http_error"}
    content_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        return {"ok": False, "error": "unsupported_media"}
    return _browser_fallback(
        final_url,
        "",
        allow_browser=True,
        max_chars=bounded_chars,
    )


def _clean_search_text(value: object, *, limit: int) -> str:
    """Retain a bounded, redacted text field from an untrusted result row."""

    plain = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    return redact_sensitive(" ".join(plain.split()), limit=limit)


def _search_searxng(query: str, base_url: str) -> list[dict[str, str]]:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("searxng_url_invalid")
    target = (parsed.path.rstrip("/") or "") + "/search?q=" + quote_plus(query) + "&format=json"
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
    try:
        conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("searxng_failed")
        payload = json.loads(response.read(1_000_001))
    finally:
        conn.close()
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    found: list[dict[str, str]] = []
    for row in rows[:30] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = public_url(str(row.get("url") or ""))
        if url:
            found.append(
                {
                    "url": url,
                    "title": _clean_search_text(row.get("title"), limit=200),
                    "snippet": _clean_search_text(
                        row.get("content") or row.get("snippet") or row.get("description"),
                        limit=MAX_SEARCH_SNIPPET_CHARS,
                    ),
                    "source": "searxng",
                }
            )
    return found[:MAX_SEARCH_RESULTS]


_SEARCH_FILLER = {
    "about",
    "and",
    "for",
    "from",
    "guide",
    "how",
    "information",
    "official",
    "technical",
    "the",
    "what",
    "with",
    "данные",
    "документация",
    "информация",
    "какие",
    "как",
    "найти",
    "официальный",
    "почему",
    "поиск",
    "про",
    "техническая",
}
_SEARXNG_ENGINES = ("brave", "google", "qwant", "yep")
_SEARCH_TIER_SCORE = {"A": 400, "B": 300, "C": 200, "D": 100, "unclassified": 0}
_SEARCH_TRACKING_PARAMETERS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "yclid", "ysclid", "mc_cid", "mc_eid", "_ga", "_gl"}
)


def _search_terms(query: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                token[:5] if len(token) > 5 else token
                for token in re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)
                if len(token) >= 3 and token not in _SEARCH_FILLER
            }
        )
    )


def _search_haystack(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("title", "snippet", "url")).casefold()


def _relevant_search_results(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep lexical matches; a failing engine can return wholly unrelated pages."""

    terms = _search_terms(query)
    if not terms:
        return rows
    required = 2 if len(terms) >= 3 else 1
    return [row for row in rows if sum(term in _search_haystack(row) for term in terms) >= required]


def _discovery_url_key(url: str) -> str:
    """Deduplicate same public result across engines without dropping meaning."""

    try:
        parsed = urlsplit(url)
        parameters = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return url.casefold()
    kept = [
        (name, value)
        for name, value in parameters
        if not (name.casefold().startswith("utm_") or name.casefold() in _SEARCH_TRACKING_PARAMETERS)
    ]
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.port and not (
        (parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)
    ):
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme.casefold(), host, parsed.path or "/", urlencode(kept, doseq=True), ""))


def _merge_snippets(left: str, right: str) -> str:
    values: list[str] = []
    for value in (left, right):
        clean = _clean_search_text(value, limit=MAX_SEARCH_SNIPPET_CHARS)
        if clean and clean.casefold() not in {item.casefold() for item in values}:
            values.append(clean)
    return " | ".join(values)[:MAX_SEARCH_SNIPPET_CHARS]


def _prepare_discovery_row(row: dict[str, Any], *, engine: str) -> dict[str, Any] | None:
    url = public_url(str(row.get("url") or ""))
    if not url:
        return None
    title = _clean_search_text(row.get("title"), limit=200)
    snippet = _clean_search_text(
        row.get("snippet") or row.get("content") or row.get("description"), limit=MAX_SEARCH_SNIPPET_CHARS
    )
    kind = "pdf" if urlsplit(url).path.casefold().endswith(".pdf") else ""
    classification = classify_source(url, title=title, kind=kind)
    return {
        "url": url,
        "title": title,
        "snippet": snippet,
        "source": _clean_search_text(row.get("source") or "searxng", limit=40),
        "engines": [engine],
        "source_class": classification.source_class,
        "source_tier": classification.source_tier,
        "source_basis": classification.source_basis,
    }


def _discovery_score(query: str, row: dict[str, Any]) -> int:
    """Rank by tier plus independent matches in title, snippet and URL."""

    score = _SEARCH_TIER_SCORE.get(str(row.get("source_tier") or ""), 0)
    title = str(row.get("title") or "").casefold()
    snippet = str(row.get("snippet") or "").casefold()
    url = str(row.get("url") or "").casefold()
    for term in _search_terms(query):
        score += 9 if term in title else 0
        score += 5 if term in snippet else 0
        score += 2 if term in url else 0
    return score


def _rank_discovered_results(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge engine output, deduplicate URLs and cap any one source domain."""

    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        prepared = _prepare_discovery_row(row, engine=str(row.get("engine") or row.get("source") or "unknown"))
        if prepared is None:
            continue
        key = _discovery_url_key(prepared["url"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = prepared
            continue
        existing["snippet"] = _merge_snippets(str(existing["snippet"]), str(prepared["snippet"]))
        existing["engines"] = sorted({*existing["engines"], *prepared["engines"]})
        # Prefer the longer title when engines disagree about the same page.
        if len(str(prepared["title"])) > len(str(existing["title"])):
            existing["title"] = prepared["title"]
    ordered = sorted(
        merged.values(),
        key=lambda row: (-_discovery_score(query, row), str(row["url"])),
    )
    result: list[dict[str, Any]] = []
    per_domain: dict[str, int] = {}
    for row in ordered:
        domain = discovery_domain(str(row["url"])) or "unknown"
        if per_domain.get(domain, 0) >= MAX_SEARCH_RESULTS_PER_DOMAIN:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        row["search_rank"] = len(result) + 1
        result.append(row)
        if len(result) >= MAX_SEARCH_RESULTS:
            break
    return result


class _DDGLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._href = ""
        self._title: list[str] = []
        self._snippet_tag = ""
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if "result__snippet" in str(attrs_map.get("class") or ""):
            self._snippet_tag = tag
            self._snippet = []
        if tag == "a" and "result__a" in str(attrs_map.get("class") or ""):
            self._href = str(attrs_map.get("href") or "")
            self._title = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            href = html.unescape(self._href)
            parsed = urlsplit(href)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
                href = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
            url = public_url(href)
            if url and len(self.rows) < MAX_SEARCH_RESULTS:
                self.rows.append(
                    {
                        "url": url,
                        "title": _clean_search_text(" ".join(self._title), limit=200),
                        "snippet": "",
                        "source": "duckduckgo",
                    }
                )
            self._href = ""
        if self._snippet_tag and tag == self._snippet_tag:
            if self.rows:
                self.rows[-1]["snippet"] = _clean_search_text(" ".join(self._snippet), limit=MAX_SEARCH_SNIPPET_CHARS)
            self._snippet_tag = ""
            self._snippet = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._title.append(data)
        if self._snippet_tag:
            self._snippet.append(data)


def search_public(query: str, *, searxng_url: str = "") -> tuple[list[dict[str, Any]], str]:
    """Search every available local engine, then rank safe public discoveries."""

    direct_url = public_url(query)
    if direct_url:
        return _rank_discovered_results(
            query,
            [{"url": direct_url, "title": "", "snippet": "", "source": "direct_url", "engine": "direct_url"}],
        ), "direct_url"
    if contains_sensitive(query):
        return [], "sensitive_query"
    if searxng_url:
        # Collect all explicitly configured engines.  One engine can drift or
        # omit a result; it must not decide the corpus alone.
        discovered: list[dict[str, Any]] = []
        for engine in _SEARXNG_ENGINES:
            try:
                found = _relevant_search_results(query, _search_searxng("!" + engine + " " + query, searxng_url))
                discovered.extend({**row, "engine": engine} for row in found)
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError, http.client.HTTPException):
                continue
        ranked = _rank_discovered_results(query, discovered)
        if ranked:
            return ranked, "searxng"
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    allowed, delay = _robots_policy(url)
    if not allowed:
        return [], "search_unavailable"
    _rate_limit("https://html.duckduckgo.com", delay)
    try:
        status, _, body, _ = _request_public(url, max_bytes=MAX_HTML_BYTES)
        if status != 200:
            return [], "search_unavailable"
        parser = _DDGLinks()
        parser.feed(body.decode("utf-8", "replace"))
        ranked = _rank_discovered_results(query, [{**row, "engine": "duckduckgo"} for row in parser.rows])
        return (ranked, "duckduckgo") if ranked else ([], "search_unavailable")
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return [], "search_unavailable"
