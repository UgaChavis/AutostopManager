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
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "AutoStop-J1/1.0 (+public research; respects robots.txt)"
MAX_HTML_BYTES = 2_000_000
MAX_PDF_BYTES = 8_000_000
MAX_TEXT_CHARS = 50_000
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
_ROBOTS: dict[str, tuple[float, RobotFileParser | None, bool, float]] = {}
_ROBOTS_LOCK = threading.Lock()
_HOST_NEXT: dict[str, float] = {}
_RATE_LOCK = threading.Lock()


def contains_sensitive(value: str) -> bool:
    """Reject sensitive user input before sending or persisting it."""

    decoded = unquote(unquote(str(value or "")))
    return bool(any(pattern.search(decoded) for pattern in (_VIN, _EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET)))


def redact_sensitive(value: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "")[: max(0, limit * 2)]
    for pattern in (_VIN, _EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET):
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def public_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or contains_sensitive(raw):
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
    url: str, *, max_bytes: int, timeout: float = 10.0, check_redirect_robots: bool = False
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
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("document_too_large")
            if headers.get("content-encoding", "identity").casefold() != "identity":
                raise ValueError("unsupported_content_encoding")
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


def fetch_document(url: str) -> dict[str, Any]:
    """Return text only; never retain source HTML/PDF bytes."""

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
    except (OSError, ValueError, TimeoutError, ssl.SSLError, http.client.HTTPException):
        return {"ok": False, "error": "fetch_failed"}
    if status in {401, 403, 429}:
        return {"ok": False, "error": "access_restricted" if status != 429 else "rate_limited"}
    if status != 200:
        return {"ok": False, "error": "http_error"}
    content_type = headers.get("content-type", "").casefold()
    if "pdf" in content_type or final_url.casefold().endswith(".pdf"):
        text = _pdf_to_text(body)
        if not text:
            return {"ok": False, "error": "pdf_extract_failed"}
        kind, title = "pdf", final_url.rsplit("/", 1)[-1][:200]
    elif "html" in content_type or "text/plain" in content_type:
        if len(body) > MAX_HTML_BYTES:
            return {"ok": False, "error": "document_too_large"}
        if "html" in content_type:
            title, text = _html_to_text(body, headers)
        else:
            title, text = "", redact_sensitive(body.decode("utf-8", "replace"))
        kind = "html" if "html" in content_type else "text"
    else:
        return {"ok": False, "error": "unsupported_media"}
    if not text or len(text) < 80:
        return {"ok": False, "error": "empty_or_dynamic"}
    lowered = text[:3000].casefold()
    if any(marker in lowered for marker in ("captcha", "введите капчу", "sign in to continue", "please log in")):
        return {"ok": False, "error": "requires_human"}
    return {
        "ok": True,
        "url": final_url,
        "title": redact_sensitive(title, limit=200),
        "text": text[:MAX_TEXT_CHARS],
        "kind": kind,
    }


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
                {"url": url, "title": redact_sensitive(str(row.get("title") or ""), limit=200), "source": "searxng"}
            )
    return found[:20]


class _DDGLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._href = ""
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attrs_map = dict(attrs)
            if "result__a" in str(attrs_map.get("class") or ""):
                self._href = str(attrs_map.get("href") or "")
                self._title = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            href = html.unescape(self._href)
            parsed = urlsplit(href)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
                href = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
            url = public_url(href)
            if url and len(self.rows) < 20:
                self.rows.append(
                    {"url": url, "title": redact_sensitive(" ".join(self._title), limit=200), "source": "duckduckgo"}
                )
            self._href = ""

    def handle_data(self, data: str) -> None:
        if self._href:
            self._title.append(data)


def search_public(query: str, *, searxng_url: str = "") -> tuple[list[dict[str, str]], str]:
    """Search local SearXNG first; then bounded public DDG HTML."""

    direct_url = public_url(query)
    if direct_url:
        return [{"url": direct_url, "title": "", "source": "direct_url"}], "direct_url"
    if contains_sensitive(query):
        return [], "sensitive_query"
    if searxng_url:
        try:
            found = _search_searxng(query, searxng_url)
            if found:
                return found, "searxng"
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError, http.client.HTTPException):
            pass
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
        return parser.rows, "duckduckgo"
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return [], "search_unavailable"
