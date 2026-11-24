"""Bounded public-web retrieval for J1.

Every public connection uses a checked DNS answer as its socket destination.
The HTTP Host and TLS server name remain the original hostname. Redirects are
resolved and checked again; proxies, cookies and authenticated pages are not used.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import html
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
import math
import os
import pwd
import re
import resource
import signal
import socket
import ssl
import string
import subprocess
import tempfile
import threading
import time
from typing import Any, NotRequired, TypedDict
from urllib.parse import (
    SplitResult,
    parse_qs,
    parse_qsl,
    quote,
    quote_plus,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from urllib.robotparser import RobotFileParser

from .j1_sources import classify_source, discovery_domain
from .j1_text import decode_http_text

USER_AGENT = "AutoStop-J1/1.0 (+public research; respects robots.txt)"
MAX_HTML_BYTES = 2_000_000
MAX_PDF_BYTES = 8_000_000
MAX_RESEARCH_PDF_BYTES = 12_000_000
MAX_TEXT_CHARS = 50_000
MAX_PUBLIC_PDF_TEXT_CHARS = 250_000
MAX_PUBLIC_FRAGMENTS = 1_000
_MAX_PDF_TEXT_BYTES = 1_000_000
_PDF_TEXT_PAGES = 300
MIXED_OCR_PAGES_PER_DOCUMENT = 3
_MIXED_OCR_RESERVATION: ContextVar[Callable[[], str | None] | None] = ContextVar(
    "j1_mixed_ocr_reservation", default=None
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_RESULTS_PER_DOMAIN = 3
MAX_SEARCH_SNIPPET_CHARS = 600
# Leave time for committing results and requeueing before the worker's 30s stop limit.
SEARCH_BUDGET_SECONDS = 18.0
_SEARXNG_ATTEMPT_SECONDS = 5.0
_SEARCH_DEADLINE: ContextVar[float | None] = ContextVar("j1_search_deadline", default=None)
_SEARCH_DNS_SLOTS = threading.BoundedSemaphore(2)
MAX_DOCUMENT_RETRIES = 1
_RETRY_DELAY_SECONDS = 0.25
_VIN = re.compile(
    r"(?<![A-HJ-NPR-Z0-9])(?:[A-HJ-NPR-Z0-9][ ._/\\-]?){16}[A-HJ-NPR-Z0-9](?![A-HJ-NPR-Z0-9])",
    re.I,
)
_PDF_BREAK = re.compile(r"[ \t\r\n]*\f[ \t\r\n\f]*")
_PDF_VIN = re.compile(
    rf"(?<![A-HJ-NPR-Z0-9])(?:[A-HJ-NPR-Z0-9](?:[ ._/\\-]|{_PDF_BREAK.pattern})?){{16}}"
    r"[A-HJ-NPR-Z0-9](?![A-HJ-NPR-Z0-9])",
    re.I,
)
# Trailing dots may be punctuation; a continuing domain label must match in full.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Z]{2,}(?![\w-]|\.+[\w-])", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+7|8|7(?=9\d{2}))[\s().-]*\d{3}(?:[\s().-]*\d){7}(?!\d)")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:api[_-]?key|token|password|secret)['\"]?\s*[:=]\s*[^\s&]{8,})"
)
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


def contains_sensitive(value: str, *, include_vin: bool = True) -> bool:
    """Reject sensitive user input before sending or persisting it."""

    decoded = _decode_percent_layers(value)
    return bool(
        (include_vin and any(_looks_like_vin(match.group()) for match in _VIN.finditer(decoded)))
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
            # Leading separators cannot change the identifier or its terminal
            # document suffix; the first nonempty identifier start covers them.
            if not _VIN_SEPARATORS.sub("", segments[start]):
                continue
            candidate = ""
            for end in range(start, len(segments)):
                candidate = segments[end] if not candidate else candidate + "/" + segments[end]
                if not _VIN_TOKEN.fullmatch(candidate):
                    break
                if _is_full_url_vin(candidate):
                    return True
                # A later suffix belongs to a new segment: it cannot remove
                # any of this prefix. Check the current document suffix first,
                # then stop once the prefix already exceeds a complete VIN.
                if len(_VIN_SEPARATORS.sub("", candidate)) > 17:
                    break
        return False

    return bool(
        any(has_vin(field) for field in fields)
        or any(pattern.search(decoded) for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET))
    )


def _hostname_contains_sensitive(host: str) -> bool:
    """Reject a full VIN used as an individual public DNS label."""

    return any(_VIN_TOKEN.fullmatch(label) and _is_full_url_vin(label) for label in host.casefold().split("."))


def redact_sensitive(
    value: str,
    *,
    limit: int = MAX_TEXT_CHARS,
    include_vin: bool = True,
    preserve_page_breaks: bool = False,
) -> str:
    text = str(value or "")[: max(0, limit * 2)]

    def replacement(match: re.Match[str]) -> str:
        breaks = match[0].count("\f") if preserve_page_breaks else 0
        return "[redacted]" + "\f" * breaks

    def replace_vin(match: re.Match[str]) -> str:
        candidate = _PDF_BREAK.sub(" ", match[0]) if preserve_page_breaks else match[0]
        if _looks_like_vin(candidate) and _is_full_url_vin(candidate):
            return replacement(match)
        return match[0]

    if include_vin:
        text = (_PDF_VIN if preserve_page_breaks else _VIN).sub(replace_vin, text)
    for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET):
        text = pattern.sub(replacement if preserve_page_breaks else "[redacted]", text)
    return text[:limit]


def complete_capped_text_end(body: str, start: int, end: int) -> int:
    """Use only the last observed line or sentence boundary before a text cap."""
    boundary = start
    for match in re.compile(r"[\r\n\f]|[.!?][ \t\u00a0\u202f]+").finditer(body, start, end):
        boundary = match.end()
    return boundary


def _bounded_public_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    redacted = redact_sensitive(text, limit=limit + 1)
    truncated = len(redacted) > limit or len(text) > 2 * (limit + 1)
    return redacted[:limit], truncated


def public_url(value: str) -> str:
    raw = str(value or "")
    if len(raw) > 2048:
        return ""
    raw = raw.strip()
    if not raw or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    try:
        raw.encode("utf-8")  # Literal surrogates are invalid URL text, unlike escaped octets.
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not re.fullmatch(r"[a-z0-9_.:-]+", host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith((".localhost", ".local", ".internal", ".onion"))
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
    if "+" in parsed.query and _url_component_contains_sensitive(parsed.query.replace("+", " ")):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # The resolver also accepts legacy IPv4 spellings, including single
        # integers and abbreviated dotted addresses. Classify them before IO.
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None and (not address.is_global or address.is_multicast):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def contains_unsafe_url(value: str) -> bool:
    """Reject private and non-web URLs even when embedded in a search phrase."""

    return any(not public_url(match.group().rstrip(".,;)]}")) for match in _EMBEDDED_URL.finditer(value))


def _public_address(host: str, port: int) -> str:
    """Reject an entire DNS answer if *any* address is non-public."""

    addresses = {str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(
        not address.is_global or address.is_multicast for address in map(ipaddress.ip_address, addresses)
    ):
        raise ValueError("unsafe_dns_answer")
    return sorted(addresses)[0]


def _deadline_timeout(timeout: float | None, deadline: float | None) -> float | None:
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("public_http_deadline")
    return min(timeout, remaining) if timeout is not None else remaining


def _search_address(host: str, port: int, deadline: float) -> str:
    """Bound the blocking resolver without leaving unbounded resolver threads."""

    _deadline_timeout(None, deadline)
    slots = _SEARCH_DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise TimeoutError("public_http_deadline")
    outcome: list[str | Exception] = []

    def resolve() -> None:
        try:
            outcome.append(_public_address(host, port))
        except (OSError, ValueError) as exc:
            outcome.append(exc)
        finally:
            slots.release()

    worker = threading.Thread(target=resolve, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        slots.release()
        raise TimeoutError("public_http_deadline") from None
    worker.join(_deadline_timeout(None, deadline))
    if worker.is_alive():
        raise TimeoutError("public_http_deadline")
    _deadline_timeout(None, deadline)
    if not outcome:
        raise OSError("public_dns_failed")
    address = outcome[0]
    if isinstance(address, Exception):
        raise address
    return address


class _DeadlineReader(io.RawIOBase):
    """Apply a wall deadline to every raw read, including headers and chunks."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._raw = sock.makefile("rb", buffering=0)
        self._sock = sock
        self._deadline = deadline
        self._idle_timeout = sock.gettimeout()

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._raw.fileno()

    def readinto(self, buffer: Any) -> int | None:
        self._sock.settimeout(_deadline_timeout(self._idle_timeout, self._deadline))
        return self._raw.readinto(buffer)

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def _deadline_response_class(deadline: float) -> type[http.client.HTTPResponse]:
    class Response(http.client.HTTPResponse):
        def __init__(
            self, sock: socket.socket, debuglevel: int = 0, method: str | None = None, url: str | None = None
        ) -> None:
            super().__init__(sock, debuglevel=debuglevel, method=method, url=url)
            assert self.fp is not None
            self.fp.close()
            self.fp = io.BufferedReader(_DeadlineReader(sock, deadline))

    return Response


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip
        self._j1_deadline: float | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), _deadline_timeout(self.timeout, self._j1_deadline)
        )
        if self._j1_deadline is not None:
            self.sock.settimeout(_deadline_timeout(self.sock.gettimeout(), self._j1_deadline))


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        self._j1_ssl_context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._j1_ssl_context)
        self._pinned_ip = ip
        self._j1_deadline: float | None = None

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port), _deadline_timeout(self.timeout, self._j1_deadline)
        )
        try:
            if self._j1_deadline is not None:
                sock.settimeout(_deadline_timeout(sock.gettimeout(), self._j1_deadline))
            self.sock = self._j1_ssl_context.wrap_socket(sock, server_hostname=self.host)
            if self._j1_deadline is not None:
                self.sock.settimeout(_deadline_timeout(self.sock.gettimeout(), self._j1_deadline))
        except Exception:
            sock.close()
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            raise


def quote_http_target(target: str) -> str:
    """Encode internationalized text without rewriting delimiters or existing escapes."""
    return quote(target, safe=string.punctuation)


def _request_target(parsed: SplitResult) -> str:
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return quote_http_target(target)


def _read_public_response_body(
    response: http.client.HTTPResponse,
    headers: dict[str, str],
    max_bytes: int,
    *,
    read_body: bool,
    allow_large_pdf: bool,
) -> bytes:
    size_header = headers.get("content-length", "")
    size = int(size_header) if size_header.isascii() and size_header.isdigit() else 0
    if size > max_bytes:
        # Check the full header values so duplicate framing and content
        # headers cannot qualify for the larger research-only budget.
        content_type = headers.get("content-type", "")
        if not (
            allow_large_pdf
            and size <= MAX_RESEARCH_PDF_BYTES
            and response.getheader("content-length") == size_header
            and "transfer-encoding" not in headers
            and response.getheader("content-type") == content_type
            and content_type.partition(";")[0].strip().casefold() == "application/pdf"
            and response.getheader("content-encoding", "identity").casefold() == "identity"
        ):
            raise ValueError("document_too_large")
        signature = b"%PDF-"
        prefix = response.read(len(signature))
        if prefix != signature:
            raise ValueError("document_too_large")
        body = prefix + response.read(size + 1 - len(prefix))
        if len(body) != size:
            raise ValueError("document_too_large")
        return body
    if headers.get("content-encoding", "identity").casefold() != "identity":
        raise ValueError("unsupported_content_encoding")
    body = response.read(max_bytes + 1) if read_body else b""
    if len(body) > max_bytes:
        raise ValueError("document_too_large")
    return body


def _request_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 10.0,
    check_redirect_robots: bool = False,
    read_body: bool = True,
    allow_large_pdf: bool = False,
) -> tuple[int, dict[str, str], bytes, str]:
    current = public_url(url)
    if not current:
        raise ValueError("unsafe_url")
    search_deadline = _SEARCH_DEADLINE.get()
    research_pdf = allow_large_pdf and read_body and max_bytes == MAX_PDF_BYTES and check_redirect_robots
    for _ in range(4):
        deadline = time.monotonic() + timeout
        if search_deadline is not None:
            deadline = min(deadline, search_deadline)
        parsed = urlsplit(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = _public_address(host, port) if search_deadline is None else _search_address(host, port, deadline)
        connection = (
            _PinnedHTTPS(host, ip, port, timeout) if parsed.scheme == "https" else _PinnedHTTP(host, ip, port, timeout)
        )
        connection._j1_deadline = deadline
        connection.response_class = _deadline_response_class(deadline)
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(
                "GET",
                _request_target(parsed),
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
            body = _read_public_response_body(
                response,
                headers,
                max_bytes,
                read_body=read_body,
                allow_large_pdf=research_pdf
                and status == 200
                and parsed.scheme == "https"
                and parsed.path.casefold().endswith(".pdf"),
            )
            return status, headers, body, current
        finally:
            try:
                if response is not None:
                    response.close()
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
            parser.parse(body.decode("utf-8-sig", "replace").splitlines())
            crawl_delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 1.0
            policy = (parser, True, min(max(float(crawl_delay), 1.0), 30.0))
        else:
            policy = (None, False, 1.0)
    except TimeoutError:
        # A short search budget does not establish this host's robots policy.
        if _SEARCH_DEADLINE.get() is not None:
            raise
        policy = (None, False, 1.0)
    except (OSError, ValueError, http.client.HTTPException):
        policy = (None, False, 1.0)
    with _ROBOTS_LOCK:
        _ROBOTS[origin] = (time.monotonic() + 3600, *policy)
    parser, available, delay = policy
    return (available and (parser is None or parser.can_fetch(USER_AGENT, url))), delay


def _rate_limit(origin: str, delay: float) -> None:
    with _RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _HOST_NEXT.get(origin, 0.0) - now)
        search_deadline = _SEARCH_DEADLINE.get()
        if search_deadline is not None and now + wait >= search_deadline:
            raise TimeoutError("public_http_deadline")
        _HOST_NEXT[origin] = now + wait + delay
    if wait:
        time.sleep(wait)


class _PageText(HTMLParser):
    def __init__(self, *, xhtml: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.title_offsets: list[int] = []
        self.links: list[str] = []
        self.headings: list[tuple[int, int, str]] = []
        self.heading_overflow = False
        self.document_ids: Counter[str] = Counter()
        self._heading: tuple[str, int, str] | None = None
        self._hidden = 0
        self._title = False
        self._templates: list[bool] = []
        self._inert_templates = 0
        self._xhtml = xhtml

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Fragment navigation resolves ids across the document, including
        # headings' non-heading siblings, but not nodes inside a template.
        anchor = ""
        if attrs and not self._templates:
            anchor = next((value or "" for name, value in attrs if name == "id"), "")
            if 0 < len(anchor) <= 200:
                self.document_ids[anchor] += 1
            else:
                anchor = ""
        if tag == "template":
            # Recognized declarative shadow roots keep their existing extraction;
            # ordinary templates remain inert, including nested shadow markup.
            mode = next((value for name, value in attrs if name == "shadowrootmode"), None)
            inert = (mode or "").lower() not in {"open", "closed"}
            self._templates.append(inert)
            self._inert_templates += inert
            return
        if self._inert_templates:
            return
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"}:
            self._hidden += 1
        if tag == "title":
            self._title = True
        if tag in _HEADING_TAGS and not self._hidden and not self._title:
            self.finish_heading()
            if len(self.headings) < MAX_PUBLIC_FRAGMENTS:
                if anchor and _url_component_contains_sensitive(anchor):
                    anchor = ""
                self._heading = (tag, len(self.parts), anchor)
            else:
                self.heading_overflow = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href and len(self.links) < 100:
                self.links.append(href)
        if tag in {"p", "br", "div", "li", "tr"} or tag in _HEADING_TAGS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template" and not self._xhtml:
            # A non-void HTML element's slash does not close its contents.
            self.handle_starttag(tag, attrs)
        else:
            super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "template":
            if self._templates:
                self._inert_templates -= self._templates.pop()
            return
        if self._inert_templates:
            return
        if tag in {"script", "style", "svg", "noscript", "nav", "footer"} and self._hidden:
            self._hidden -= 1
        if tag == "title":
            self._title = False
        if tag in _HEADING_TAGS and not self._hidden:
            if self._heading is not None and tag == self._heading[0]:
                self.finish_heading()
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._inert_templates:
            return
        if self._title:
            self.title_parts.append(data)
            if not self._hidden:
                self.title_offsets.append(len(self.parts))
        if not self._hidden:
            self.parts.append(data)

    def finish_heading(self) -> None:
        if self._heading is not None:
            _, start, anchor = self._heading
            self.headings.append((start, len(self.parts), anchor))
            self._heading = None


def _normalize_html_text(parts: list[str]) -> str:
    text = re.sub(r"[ \t]+", " ", "".join(parts))
    return re.sub(r"\n[\s\n]*\n", "\n", text).strip()


def _html_document(body: bytes, headers: dict[str, str]) -> tuple[str, str, list[dict[str, Any]], bool]:
    decoded = decode_http_text(body, headers.get("content-type", ""))
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    parser = _PageText(xhtml=media_type == "application/xhtml+xml")
    parser.feed(decoded)
    parser.close()
    parser.finish_heading()
    title = redact_sensitive(" ".join(" ".join(parser.title_parts).split()), limit=200)
    if not parser.headings:
        body_text, truncated = _bounded_public_text(_normalize_html_text(parser.parts))
        return title, body_text, [], truncated

    # Flat extraction historically includes the title. For section citations
    # it is document metadata and must not create a searchable first section.
    for position in parser.title_offsets:
        parser.parts[position] = ""
    first, *_ = parser.headings[0]
    sections = [(_normalize_html_text(parser.parts[:first]), "", "")]
    for index, (start, heading_end, anchor) in enumerate(parser.headings):
        end = parser.headings[index + 1][0] if index + 1 < len(parser.headings) else len(parser.parts)
        sections.append(
            (
                _normalize_html_text(parser.parts[start:end]),
                _normalize_html_text(parser.parts[start:heading_end]),
                anchor,
            )
        )
    sections = [(body, heading, anchor) for body, heading, anchor in sections if body]
    if sections and (parser.heading_overflow or len(sections) > MAX_PUBLIC_FRAGMENTS):
        # Keep the remaining text searchable without attributing later
        # sections to the last heading that fit in the index.
        last = min(len(sections), MAX_PUBLIC_FRAGMENTS) - 1
        sections[last:] = [("\n".join(body for body, _, _ in sections[last:]), "", "")]
    text, truncated = _bounded_public_text("\n".join(body for body, _, _ in sections))
    fragments: list[dict[str, Any]] = []
    offset = 0
    for number, (section_text, heading, anchor) in enumerate(sections, 1):
        fragment = redact_sensitive(section_text, limit=len(text) - offset)
        end = offset + len(fragment)
        if not fragment:
            break
        if text[offset:end] != fragment:
            # A sensitive sequence can span a heading boundary. Keep the
            # jointly redacted text and omit fragment offsets in that case.
            return title, text, [], truncated
        label = redact_sensitive(heading, limit=min(200, len(fragment)))
        fragments.append(
            {
                "page": number,
                "fragment_kind": "html_section",
                "section": label if fragment.startswith(label) else "",
                "anchor": anchor if parser.document_ids[anchor] == 1 else "",
                "start": offset,
                "end": end,
            }
        )
        offset = end + 1
    return title, text, fragments, truncated


def _html_to_text(body: bytes, headers: dict[str, str]) -> tuple[str, str]:
    title, text, _, _ = _html_document(body, headers)
    return title, text


def _pdf_to_text(body: bytes) -> str:
    def restrict() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_PDF_TEXT_BYTES, _MAX_PDF_TEXT_BYTES))
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            os.setgroups([])
            os.setgid(nobody.pw_gid)
            os.setuid(nobody.pw_uid)

    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["/usr/bin/pdftotext", "-f", "1", "-l", str(_PDF_TEXT_PAGES), "-layout", "-", "-"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=restrict,
            )
            try:
                process.communicate(input=body, timeout=15)
            except subprocess.TimeoutExpired:
                from .j1_vin_documents import _kill_parser

                _kill_parser(process)
                return ""
            if process.returncode not in {0, -signal.SIGXFSZ}:
                return ""
            output.seek(0)
            extracted = output.read(_MAX_PDF_TEXT_BYTES)
    except (OSError, subprocess.SubprocessError, KeyError):
        return ""
    return redact_sensitive(
        extracted.decode("utf-8", "replace"), limit=MAX_PUBLIC_PDF_TEXT_CHARS, preserve_page_breaks=True
    )


def complete_pdf_fragment_end(body: str, start: int, end: int) -> int:
    """Exclude the unfinished tail of a PDF page when using it as evidence."""
    if start == end or body[end - 1] in "\f\n" or body.find("\f", end) != -1 or body.find("\n", end) != -1:
        return end
    return max(start, body.rfind("\n", start, end))


def _pdf_fragments(
    text: str, ocr_pages: frozenset[int] = frozenset(), truncated_pages: frozenset[int] = frozenset()
) -> list[dict[str, Any]]:
    fragments = []
    offset = 0
    for number, body in enumerate(text.split("\f", _PDF_TEXT_PAGES), 1):
        start = offset + len(body) - len(body.lstrip())
        end = offset + len(body.rstrip())
        truncated = number in truncated_pages or (start < end and offset + len(body) == len(text))
        if start < end or truncated:
            fragments.append(
                {
                    "page": number,
                    "fragment_kind": "pdf_page",
                    "start": min(start, end),
                    "end": end,
                    "extraction_method": "pdf_ocr" if number in ocr_pages else "pdf_text",
                    "truncated": truncated,
                }
            )
        offset += len(body) + 1
    return fragments


@contextmanager
def mixed_ocr_budget(reserve_page: Callable[[], str | None]) -> Iterator[None]:
    """Enable selected-page OCR only while the public worker reads a document."""
    token = _MIXED_OCR_RESERVATION.set(reserve_page)
    try:
        yield
    finally:
        _MIXED_OCR_RESERVATION.reset(token)


def _extract_mixed_pdf(body: bytes, text: str, reserve_page: Callable[[], str | None]) -> dict[str, Any]:
    from .j1_ocr import complete_ocr_lines, extract_mixed_pdf_page

    pages = text.split("\f", _PDF_TEXT_PAGES)
    # The trailing chunk may be a truncated page. A form feed proves the
    # preceding chunk represents the entire physical page.
    missing = [index for index, page in enumerate(pages[:-1]) if not page.strip()]
    limitations = set()
    if len(pages) > _PDF_TEXT_PAGES:
        limitations.add("pdf_page_limit_reached")
    if len(text) == MAX_PUBLIC_PDF_TEXT_CHARS:
        limitations.add("pdf_text_limit_reached")
    if pages[-1].strip():
        limitations.add("pdf_text_partial")
    if len(missing) > MIXED_OCR_PAGES_PER_DOCUMENT:
        limitations.add("ocr_document_budget")
    merged_text = text
    ocr_pages: set[int] = set()
    truncated_pages: set[int] = set()
    for index in missing[:MIXED_OCR_PAGES_PER_DOCUMENT]:
        remaining_chars = MAX_PUBLIC_PDF_TEXT_CHARS - len(merged_text)
        if remaining_chars <= 0:
            limitations.add("ocr_text_limit")
            break
        refusal = reserve_page()
        if refusal:
            limitations.add(refusal)
            break
        page_text, problem = extract_mixed_pdf_page(body, index + 1)
        if problem:
            limitations.add(problem)
        if problem == "ocr_text_limit":
            truncated_pages.add(index + 1)
        if len(page_text) > remaining_chars:
            limitations.add("ocr_text_limit")
            truncated_pages.add(index + 1)
            prefix = page_text[:remaining_chars]
            page_text = prefix.rstrip() if page_text[remaining_chars] == "\n" else complete_ocr_lines(prefix)
        if not page_text:
            continue
        native_page = pages[index]
        pages[index] = page_text
        # Redaction may expand a short match. Check the whole redacted candidate
        # above the storage cap so a mask cannot displace trailing native text.
        candidate = redact_sensitive("\f".join(pages), limit=MAX_PUBLIC_PDF_TEXT_CHARS * 2, preserve_page_breaks=True)
        if len(candidate) > MAX_PUBLIC_PDF_TEXT_CHARS:
            pages[index] = native_page
            limitations.add("ocr_text_limit")
            truncated_pages.add(index + 1)
            continue
        merged_text = candidate
        ocr_pages.add(index + 1)
    return {
        "ok": True,
        "text": merged_text,
        "extraction_method": "pdf_mixed" if ocr_pages else "pdf_text",
        "ocr_pages": frozenset(ocr_pages | truncated_pages),
        "truncated_pages": frozenset(truncated_pages),
        "pdf_limitations": sorted(limitations),
    }


def _is_challenge_text(text: str) -> bool:
    opening = text[:3000].casefold()
    return any(marker in opening for marker in ("captcha", "введите капчу", "sign in to continue", "please log in"))


def _extract_pdf(body: bytes) -> dict[str, Any]:
    text = _pdf_to_text(body)
    if text.strip():
        reserve_page = _MIXED_OCR_RESERVATION.get()
        # A native PDF already readable without OCR keeps its access decision.
        # Scanned workshop instructions may describe a login without gating access to the PDF.
        if reserve_page is not None and not _is_challenge_text(text):
            return _extract_mixed_pdf(body, text, reserve_page)
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
    path = urlsplit(final_url).path
    if (
        "pdf" in content_type
        or body.startswith(b"%PDF-")
        or ("html" not in content_type and path.lower().endswith(".pdf"))
    ):
        extracted = _extract_pdf(body)
        if extracted.get("ok") is not True:
            return extracted
        filename = path.rsplit("/", 1)[-1]
        decoded_name = unquote(filename, errors="surrogateescape")
        method = extracted["extraction_method"]
        return {
            "ok": True,
            "kind": "pdf",
            "title": (decoded_name if decoded_name.isprintable() else filename)[:200],
            "text": extracted["text"],
            "extraction_method": method,
            "fragments": _pdf_fragments(
                extracted["text"],
                extracted.get("ocr_pages", frozenset()),
                extracted.get("truncated_pages", frozenset()),
            )
            if method in {"pdf_text", "pdf_mixed"}
            else [],
            "pdf_limitations": extracted.get("pdf_limitations", []),
        }
    if "html" in content_type or "text/plain" in content_type:
        if len(body) > MAX_HTML_BYTES:
            return {"ok": False, "error": "document_too_large"}
        if "html" in content_type:
            title, text, fragments, truncated = _html_document(body, headers)
            kind, method = "html", "html_text"
        else:
            title, kind, method = "", "text", "plain_text"
            text, truncated = _bounded_public_text(decode_http_text(body, headers.get("content-type", "")))
            fragments = []
        return {
            "ok": True,
            "kind": kind,
            "title": title,
            "text": text,
            "extraction_method": method,
            "fragments": fragments,
            **({"body_truncated": True} if truncated else {}),
        }
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
    raw_text = str(rendered.get("text") or "")
    rendered_text, truncated = _bounded_public_text(raw_text, bounded_chars)
    if not rendered_url or len(rendered_text) < 80:
        return {"ok": False, "error": "browser_response_invalid", "browser_attempted": True}
    if _is_challenge_text(rendered_text):
        return {"ok": False, "error": "requires_human", "browser_attempted": True}
    return {
        "ok": True,
        "url": rendered_url,
        "title": redact_sensitive(str(rendered.get("title") or title), limit=200),
        "text": rendered_text,
        "extraction_method": "browser_dom",
        "browser_attempted": True,
        **({"body_truncated": True} if truncated or len(raw_text) >= bounded_chars else {}),
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


def _fetch_document_once(url: str, *, allow_browser: bool = True, allow_large_pdf: bool = False) -> dict[str, Any]:
    """Run one fail-closed document retrieval attempt."""

    safe_url = public_url(url)
    if not safe_url:
        return {"ok": False, "error": "unsafe_url"}
    allowed, delay = _robots_policy(safe_url)
    if not allowed:
        return {"ok": False, "error": "robots_disallowed"}
    parsed = urlsplit(safe_url)
    _rate_limit(f"{parsed.scheme}://{parsed.netloc}", delay)
    research_options = {"allow_large_pdf": True} if allow_large_pdf else {}
    try:
        status, headers, body, final_url = _request_public(
            safe_url, max_bytes=MAX_PDF_BYTES, check_redirect_robots=True, **research_options
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
    short_pdf = (
        _MIXED_OCR_RESERVATION.get() is not None and extracted["kind"] == "pdf" and bool(extracted.get("fragments"))
    )
    if len(extracted["text"]) < 80 and short_pdf:
        extracted["pdf_limitations"] = sorted({*extracted.get("pdf_limitations", []), "short_pdf_text"})
    elif len(extracted["text"]) < 80:
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
        extracted.pop("body_truncated", None)
        extracted.update(fallback)
        extracted["fragments"] = []
        final_url = fallback["url"]
    text = extracted["text"]
    if extracted["extraction_method"] != "pdf_mixed" and _is_challenge_text(text):
        return {
            "ok": False,
            "error": "requires_human",
            "extraction_method": extracted["extraction_method"],
            "browser_attempted": bool(extracted.get("browser_attempted")),
        }
    result: dict[str, Any] = {
        "ok": True,
        "url": final_url,
        "title": redact_sensitive(extracted["title"], limit=200),
        "text": text,
        "kind": extracted["kind"],
        "extraction_method": extracted["extraction_method"],
        "browser_attempted": bool(extracted.get("browser_attempted")),
    }
    if extracted.get("fragments"):
        result["fragments"] = extracted["fragments"]
    if extracted.get("pdf_limitations"):
        result["pdf_limitations"] = extracted["pdf_limitations"]
    if extracted.get("body_truncated"):
        result["body_truncated"] = True
    return result


def fetch_document(url: str, *, allow_browser: bool = True, allow_large_pdf: bool = False) -> dict[str, Any]:
    """Return bounded public text with at most one transient-error retry.

    Robots, privacy, authentication, CAPTCHA and validation failures never
    retry.  The retry starts a new complete guarded request, so DNS and robots
    controls are re-applied before any second connection. Public research
    jobs may opt into the larger download budget for verified PDF responses.
    """

    first = _fetch_document_once(url, allow_browser=allow_browser, allow_large_pdf=allow_large_pdf)
    if first.get("ok") is True or str(first.get("error") or "") not in _RETRYABLE_DOCUMENT_ERRORS:
        return first
    if first.get("error") == "rate_limited":
        time.sleep(_bounded_retry_delay(first.get("retry_after_seconds")))
    second = _fetch_document_once(url, allow_browser=allow_browser, allow_large_pdf=allow_large_pdf)
    if first.get("browser_attempted") is True:
        second["browser_attempted"] = True
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


def parse_searxng_endpoint(base_url: str) -> tuple[str, int, str]:
    """Validate the configured loopback endpoint without opening a connection."""
    try:
        parsed = urlsplit(base_url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("searxng_url_invalid") from None
    if (
        parsed.scheme != "http"
        or host is None
        or host not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("searxng_url_invalid")
    return host, port or 80, parsed.path.rstrip("/")


class SearchAttempt(TypedDict):
    provider: str
    engine: str
    outcome: str
    http_status: NotRequired[int]
    received: NotRequired[int]
    candidates: NotRequired[int]
    holdoff_reason: NotRequired[str]
    upstream_failures: NotRequired[list[dict[str, str]]]


MAX_SEARCH_ATTEMPTS = 6
_MAX_UPSTREAM_FAILURES = 8
_KNOWN_UPSTREAM_ENGINES = frozenset({"bing", "yahoo", "google", "google cse", "duckduckgo", "wikipedia", "vuhuv"})
_SEARCH_ATTEMPTS: ContextVar[list[SearchAttempt] | None] = ContextVar("j1_search_attempts", default=None)
_SEARXNG_ATTEMPT: ContextVar[SearchAttempt | None] = ContextVar("j1_searxng_attempt", default=None)
_SEARCH_HOLDOFFS: ContextVar[dict[str, str] | None] = ContextVar("j1_search_holdoffs", default=None)
_SEARCH_HOLDOFF_REASONS = frozenset({"access_challenge", "access_restricted", "rate_limited"})


@contextmanager
def capture_search_attempts() -> Iterator[list[SearchAttempt]]:
    """Capture bounded search diagnostics in this call context.

    Direct URLs and rejected inputs do not make a provider attempt. Records
    hold fixed outcomes and counts, never queries, URLs, response text or errors.
    Empty and no_relevant_results are clean only without upstream_failures.
    """
    attempts: list[SearchAttempt] = []
    token = _SEARCH_ATTEMPTS.set(attempts)
    try:
        yield attempts
    finally:
        _SEARCH_ATTEMPTS.reset(token)


@contextmanager
def automotive_search_routing(holdoffs: dict[str, str]) -> Iterator[None]:
    """Prefer default SearXNG and share terminal route failures within one job run."""
    token = _SEARCH_HOLDOFFS.set(holdoffs)
    try:
        yield
    finally:
        _SEARCH_HOLDOFFS.reset(token)


def _record_search_attempt(attempt: SearchAttempt) -> None:
    attempts = _SEARCH_ATTEMPTS.get()
    if attempts is not None and len(attempts) < MAX_SEARCH_ATTEMPTS:
        attempts.append(attempt)
    holdoffs = _SEARCH_HOLDOFFS.get()
    if holdoffs is not None and attempt["outcome"] in _SEARCH_HOLDOFF_REASONS:
        holdoffs.setdefault(attempt["provider"], attempt["outcome"])


def _search_route_held_off(attempt: SearchAttempt) -> bool:
    holdoffs = _SEARCH_HOLDOFFS.get()
    reason = holdoffs.get(attempt["provider"]) if holdoffs is not None else None
    if reason is None or reason not in _SEARCH_HOLDOFF_REASONS:
        return False
    attempt["outcome"] = "held_off"
    attempt["holdoff_reason"] = reason
    _record_search_attempt(attempt)
    return True


def _search_http_outcome(status: int) -> str:
    if status in (401, 403):
        return "access_restricted"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "http_server_error"
    return "http_error"


def _upstream_failure_reason(detail: str) -> str:
    message = detail[:128].casefold()
    if "too many requests" in message or "rate limit" in message:
        return "rate_limited"
    if "captcha" in message:
        return "access_challenge"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "access denied" in message or "forbidden" in message:
        return "access_restricted"
    if "http error" in message:
        return "http_error"
    if "network" in message or "connection" in message:
        return "transport_error"
    if "suspended" in message:
        return "suspended"
    return "upstream_unavailable"


def _searxng_upstream_failures(payload: dict[str, Any]) -> list[dict[str, str]]:
    unresponsive = payload.get("unresponsive_engines")
    if not isinstance(unresponsive, list):
        return []
    failures: dict[tuple[str, str], dict[str, str]] = {}
    for item in unresponsive[:32]:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(field, str) for field in item):
            continue
        engine_name = item[0][:32].casefold()
        engine = engine_name if engine_name in _KNOWN_UPSTREAM_ENGINES else "other"
        reason = _upstream_failure_reason(item[1])
        failures[(engine, reason)] = {"engine": engine, "reason": reason}
        if len(failures) >= _MAX_UPSTREAM_FAILURES:
            break
    return list(failures.values())


def _search_searxng(query: str, base_url: str) -> list[dict[str, str]]:
    attempt = _SEARXNG_ATTEMPT.get()
    try:
        host, port, prefix = parse_searxng_endpoint(base_url)
    except ValueError:
        if attempt is not None:
            attempt["outcome"] = "configuration_invalid"
        raise
    target = prefix + "/search?q=" + quote_plus(query) + "&format=json"
    search_deadline = _SEARCH_DEADLINE.get()
    started = time.monotonic()
    deadline = started + 10 if search_deadline is None else min(started + _SEARXNG_ATTEMPT_SECONDS, search_deadline)
    conn = http.client.HTTPConnection(host, port, timeout=_deadline_timeout(None, deadline))
    conn.response_class = _deadline_response_class(deadline)
    response: http.client.HTTPResponse | None = None
    try:
        conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        response = conn.getresponse()
        if attempt is not None and 100 <= response.status <= 599:
            attempt["http_status"] = response.status
        if response.status != 200:
            if attempt is not None:
                attempt["outcome"] = _search_http_outcome(response.status)
            raise ValueError("searxng_failed")
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("searxng_response_too_large")
        payload = json.loads(body)
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            conn.close()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if attempt is not None and isinstance(payload, dict):
        failures = _searxng_upstream_failures(payload)
        if failures:
            attempt["upstream_failures"] = failures
    if not isinstance(rows, list):
        if attempt is not None:
            attempt["outcome"] = "invalid_response"
        return []
    if attempt is not None:
        attempt["received"] = min(len(rows), 30)
    found: list[dict[str, str]] = []
    for row in rows[:30]:
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
# Explicit SearXNG bangs may use engines excluded from default searches.
# Bing and Yahoo provide separate candidates; failures are isolated below.
_SEARXNG_ENGINES = ("bing", "yahoo")
_SEARXNG_DEFAULT = "searxng_default"
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


_SEARCH_WORD = re.compile(r"[^\W_]+(?:[-‐‑‒–—−][^\W_]+)*")


def _has_qualified_search_lead(query: str, rows: list[dict[str, Any]]) -> bool:
    """Require more topic coverage to skip a search than to retain a lead."""
    terms = _search_terms(query)
    minimum = (2 * len(terms) + 2) // 3
    anchors = {
        word
        for word in _SEARCH_WORD.findall(query.casefold())
        if any(char.isdigit() for char in word) and (len(word) >= 5 or any(char.isalpha() for char in word))
    }
    if not terms and not anchors:
        return False
    for row in rows:
        haystack = _search_haystack(row)
        if sum(term in haystack for term in terms) >= minimum and anchors.issubset(_SEARCH_WORD.findall(haystack)):
            return True
    return False


def _discovery_url_key(url: str) -> str:
    """Deduplicate same public result across engines without dropping meaning."""

    try:
        url.encode("utf-8")
        parsed = urlsplit(url)
        # Legacy URL query bytes must not collapse to the same replacement character.
        parameters = parse_qsl(parsed.query, keep_blank_values=True, errors="surrogateescape")
    except UnicodeError:
        return ""
    except ValueError:
        return url.casefold()
    kept = [
        (name, value)
        for name, value in parameters
        if not (name.casefold().startswith("utm_") or name.casefold() in _SEARCH_TRACKING_PARAMETERS)
    ]
    host = (parsed.hostname or "").casefold().rstrip(".")
    host = f"[{host}]" if ":" in host else host
    if parsed.port and not (
        (parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)
    ):
        host += f":{parsed.port}"
    return urlunsplit(
        (parsed.scheme.casefold(), host, parsed.path or "/", urlencode(kept, doseq=True, errors="surrogateescape"), "")
    )


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
        self.no_results = False
        self.saw_result_link = False
        self._href = ""
        self._title: list[str] = []
        self._snippet_tag = ""
        self._snippet: list[str] = []
        self._snippet_owner: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        classes = str(attrs_map.get("class") or "")
        if {"result--no-result", "no-results"}.intersection(classes.split()):
            self.no_results = True
        if "result__snippet" in classes:
            self._snippet_tag = tag
            self._snippet = []
        if tag == "a" and "result__a" in classes:
            self._snippet_owner = None
            self._href = str(attrs_map.get("href") or "")
            self.saw_result_link |= bool(self._href)
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
                self._snippet_owner = self.rows[-1]
            self._href = ""
        if self._snippet_tag and tag == self._snippet_tag:
            if self._snippet_owner is not None:
                self._snippet_owner["snippet"] = _clean_search_text(
                    " ".join(self._snippet), limit=MAX_SEARCH_SNIPPET_CHARS
                )
            self._snippet_tag = ""
            self._snippet = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._title.append(data)
        if self._snippet_tag:
            self._snippet.append(data)


def search_public(
    query: str, *, searxng_url: str = "", stop_requested: Callable[[], bool] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Search configured local engines, then rank safe public discoveries."""

    direct_url = public_url(query)
    if direct_url:
        return _rank_discovered_results(
            query,
            [{"url": direct_url, "title": "", "snippet": "", "source": "direct_url", "engine": "direct_url"}],
        ), "direct_url"
    if contains_sensitive(query):
        return [], "sensitive_query"
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    token = _SEARCH_DEADLINE.set(deadline)
    try:
        return _search_discovery(query, searxng_url, deadline, stop_requested or (lambda: False))
    finally:
        _SEARCH_DEADLINE.reset(token)


def _try_searxng(query: str, searxng_url: str, engine: str) -> list[dict[str, str]]:
    attempt = SearchAttempt(provider="searxng", engine=engine, outcome="search_error")
    if _search_route_held_off(attempt):
        return []
    capture_details = _SEARCH_ATTEMPTS.get() is not None or _SEARCH_HOLDOFFS.get() is not None
    token = _SEARXNG_ATTEMPT.set(attempt if capture_details else None)
    try:
        engine_query = query if engine == _SEARXNG_DEFAULT else f"!{engine} {query}"
        rows = _search_searxng(engine_query, searxng_url)
        if attempt["outcome"] == "search_error":
            found = _relevant_search_results(query, rows)
            attempt["candidates"] = len(found)
            if found:
                attempt["outcome"] = "results"
            else:
                attempt["outcome"] = "no_relevant_results" if rows or attempt.get("received") else "empty"
            return found
    except TimeoutError:
        attempt["outcome"] = "timeout"
    except (OSError, http.client.HTTPException):
        attempt["outcome"] = "transport_error"
    except ValueError:
        if attempt["outcome"] == "search_error":
            attempt["outcome"] = "invalid_response"
    finally:
        _SEARXNG_ATTEMPT.reset(token)
        _record_search_attempt(attempt)
    return []


def _search_discovery(
    query: str, searxng_url: str, deadline: float, stop_requested: Callable[[], bool]
) -> tuple[list[dict[str, Any]], str]:
    if stop_requested():
        return [], "search_interrupted"
    if searxng_url:
        default = (_SEARXNG_DEFAULT,)
        automotive = _SEARCH_HOLDOFFS.get() is not None
        engine_groups = (default, _SEARXNG_ENGINES) if automotive else (_SEARXNG_ENGINES, default)
        discovered: list[dict[str, Any]] = []
        ranked: list[dict[str, Any]] = []
        for group in engine_groups:
            for engine in group:
                if stop_requested():
                    return _rank_discovered_results(query, discovered), "search_interrupted"
                if time.monotonic() >= deadline:
                    break
                found = _try_searxng(query, searxng_url, engine)
                discovered.extend({**row, "engine": engine} for row in found)
            ranked = _rank_discovered_results(query, discovered)
            qualified = [row for row in ranked if row["source_tier"] in {"A", "B", "C"}]
            if qualified and (not automotive or _has_qualified_search_lead(query, qualified)):
                break
        if ranked:
            return ranked, "searxng"
    return _search_duckduckgo(query, deadline, stop_requested)


def _search_duckduckgo(
    query: str, deadline: float, stop_requested: Callable[[], bool]
) -> tuple[list[dict[str, Any]], str]:
    if stop_requested():
        return [], "search_interrupted"
    if time.monotonic() >= deadline:
        return [], "search_timeout"
    attempt = SearchAttempt(provider="duckduckgo", engine="duckduckgo", outcome="interrupted")
    if _search_route_held_off(attempt):
        return [], "search_unavailable"
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    try:
        allowed, delay = _robots_policy(url)
        if not allowed:
            attempt["outcome"] = "robots_denied"
            return [], "search_unavailable"
        if stop_requested():
            return [], "search_interrupted"
        if time.monotonic() >= deadline:
            attempt["outcome"] = "timeout"
            return [], "search_timeout"
        _rate_limit("https://html.duckduckgo.com", delay)
        if stop_requested():
            return [], "search_interrupted"
        status, _, body, _ = _request_public(url, max_bytes=MAX_HTML_BYTES)
        if 100 <= status <= 599:
            attempt["http_status"] = status
        # Challenge forms also appear in HTTP 200/202 responses. They are an
        # access denial, and no result parsing or follow-on request is made.
        prefix = body[:256_000].lower()
        if (
            b'id="challenge-form"' in prefix
            or b'class="anomaly-modal' in prefix
            or (status == 202 and b"captcha" in prefix)
        ):
            attempt["outcome"] = "access_challenge"
            return [], "search_unavailable"
        if status != 200:
            attempt["outcome"] = _search_http_outcome(status)
            return [], "search_unavailable"
        parser = _DDGLinks()
        parser.feed(body.decode("utf-8", "replace"))
        ranked = _rank_discovered_results(query, [{**row, "engine": "duckduckgo"} for row in parser.rows])
        attempt["candidates"] = len(ranked)
        if ranked:
            attempt["outcome"] = "results"
            return ranked, "duckduckgo"
        if parser.saw_result_link:
            attempt["outcome"] = "no_relevant_results"
        else:
            attempt["outcome"] = "empty" if parser.no_results else "invalid_response"
        return [], "search_unavailable"
    except TimeoutError:
        attempt["outcome"] = "timeout"
        return [], "search_timeout"
    except (OSError, http.client.HTTPException):
        attempt["outcome"] = "transport_error"
        return [], "search_unavailable"
    except ValueError:
        attempt["outcome"] = "invalid_response"
        return [], "search_unavailable"
    finally:
        _record_search_attempt(attempt)
