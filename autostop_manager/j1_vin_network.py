"""Transient, one-VIN public-web transport; generic J1 privacy stays unchanged.

The caller owns job authorization and the discovered-source policy. This module
allows only the exact scoped VIN, never logs inputs, and does not submit decoder
forms or use VIN APIs. Raw response bytes belong only to the temporary pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import html
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from . import j1_fetch
from .j1_sources import classify_source, discovery_domain

_VIN_TOKEN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_ENGINES = frozenset({"bing", "yahoo", "duckduckgo"})
_SECRET_FIELD = re.compile(
    r"(?i)\b(?:api[_-]?key|key|(?:access|refresh)[_-]?token|token|password|passwd|secret|authorization|auth|sid|session(?:[_-]?id)?|cookie)\s*[:=]\s*[^\s&]+"
)
_INTERNATIONAL_PHONE = re.compile(r"(?<!\w)\+\d(?:[\s().-]*\d){7,14}(?!\d)")
_MASK = "[scoped_vin]"
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_SEARCH_BYTES = 1_000_000
_PinnedHTTP = j1_fetch._PinnedHTTP
_PinnedHTTPS = j1_fetch._PinnedHTTPS
_public_address = j1_fetch._public_address


def normalize_vin(vin: str) -> str:
    """Check syntax only: US check-digit rules are not universal VIN rules."""

    if not isinstance(vin, str):
        raise ValueError("invalid_vin")
    normalized = vin.strip().upper()
    if not _VIN_TOKEN.fullmatch(normalized):
        raise ValueError("invalid_vin")
    return normalized


@dataclass(frozen=True)
class VinScope:
    vin: str
    job_id: str
    expires_at: float
    allowed_engines: tuple[str, ...] = ("bing", "yahoo", "duckduckgo")
    active_check: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vin", normalize_vin(self.vin))
        if not isinstance(self.job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", self.job_id):
            raise ValueError("invalid_scope")
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)):
            raise ValueError("invalid_scope")
        if not math.isfinite(self.expires_at):
            raise ValueError("invalid_scope")
        if not isinstance(self.allowed_engines, tuple) or any(
            engine not in _ENGINES for engine in self.allowed_engines
        ):
            raise ValueError("invalid_engines")
        if len(set(self.allowed_engines)) != len(self.allowed_engines):
            raise ValueError("invalid_engines")
        if self.active_check is not None and not callable(self.active_check):
            raise ValueError("invalid_scope")


def _active(scope: VinScope) -> bool:
    if not isinstance(scope, VinScope) or time.time() >= scope.expires_at:
        return False
    try:
        return scope.active_check is None or scope.active_check() is True
    except Exception:  # noqa: BLE001 - cancellation-state read failure must fail closed.
        return False


def _require_active(scope: VinScope) -> None:
    if not _active(scope):
        raise ValueError("scope_expired")


def _decoded(value: str) -> str:
    decoded = unicodedata.normalize("NFKC", value)
    # Each decoding pass shortens escaped input; the bounded input length also
    # bounds work, including identifiers encoded more than 64 times.
    for _ in range(len(decoded) + 1):
        next_value = html.unescape(unquote(decoded))
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _has_controls(value: str) -> bool:
    return any(ord(char) < 32 or unicodedata.category(char) == "Cf" for char in value)


def _mask_target(value: str, scope: VinScope, marker: str = _MASK) -> str:
    return re.sub(r"(?<![A-Z0-9])" + re.escape(scope.vin) + r"(?![A-Z0-9])", marker, value, flags=re.I)


def _has_secret(value: str) -> bool:
    return bool(_SECRET_FIELD.search(value) or _INTERNATIONAL_PHONE.search(value))


def _is_api_url(url: str) -> bool:
    # Published API documentation remains a valid source; API operation routes
    # and API hosts are not public-document fetch targets for this adapter.
    parsed = urlsplit(url)
    return bool(
        (parsed.hostname or "").casefold().startswith("api.")
        or re.search(r"/(?:api|graphql|rest)(?:/|$)", _decoded(parsed.path), re.I)
    )


def safe_query(query: str, scope: VinScope) -> bool:
    if not _active(scope) or not isinstance(query, str) or not query.strip() or len(query) > 2048:
        return False
    decoded = _decoded(query)
    if _has_controls(decoded) or "!" in decoded:
        return False
    masked = _mask_target(decoded, scope)
    # '+' also separates URL-encoded VIN characters, rather than hiding them.
    if j1_fetch.contains_sensitive(masked.replace("+", " ")) or _has_secret(masked):
        return False
    return all(safe_url(match.group().rstrip(".,;)]}"), scope) for match in j1_fetch._EMBEDDED_URL.finditer(decoded))


def safe_url(url: str, scope: VinScope) -> str:
    if not _active(scope) or not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw) or "\\" in raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = _decoded(parsed.hostname or "")
        path, query, fragment = (_decoded(value) for value in (parsed.path, parsed.query, parsed.fragment))
        if any(_has_controls(value) for value in (host, path, query, fragment)):
            return ""
        if (
            j1_fetch._hostname_contains_sensitive(host)
            or any(
                pattern.search(host)
                for pattern in (j1_fetch._EMAIL, j1_fetch._PHONE, j1_fetch._SECRET, j1_fetch._JWT, j1_fetch._API_SECRET)
            )
            or _has_secret(host)
            or j1_fetch._url_component_contains_sensitive(fragment)
        ):
            return ""
        if _has_secret(fragment):
            return ""
        if any(
            j1_fetch._url_component_contains_sensitive(_mask_target(value, scope).replace("+", " "))
            or _has_secret(_mask_target(value, scope))
            for value in (path, query)
        ):
            return ""
        masked_url = urlunsplit(
            (parsed.scheme, parsed.netloc, _mask_target(path, scope), _mask_target(query, scope), fragment)
        )
        if not j1_fetch.public_url(masked_url) or _is_api_url(raw):
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        return ""


def _remaining(scope: VinScope, deadline: float) -> float:
    _require_active(scope)
    remaining = min(deadline - time.monotonic(), scope.expires_at - time.time())
    if remaining <= 0:
        raise TimeoutError("fetch_timeout")
    return remaining


def _resolve_public(host: str, port: int, scope: VinScope, deadline: float) -> str:
    """Bound DNS as well as socket I/O; a late DNS result cannot connect."""

    result: list[str | BaseException] = []

    def resolve() -> None:
        try:
            _require_active(scope)
            result.append(_public_address(host, port))
        except (OSError, ValueError) as exc:
            result.append(exc)

    _remaining(scope, deadline)
    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(_remaining(scope, deadline))
    _remaining(scope, deadline)
    if worker.is_alive() or not result:
        raise TimeoutError("fetch_timeout")
    if isinstance(result[0], BaseException):
        if isinstance(result[0], ValueError):
            raise ValueError("unsafe_dns_answer") from None
        raise OSError("fetch_failed") from None
    address = result[0]
    if not ipaddress.ip_address(address).is_global:
        raise ValueError("unsafe_dns_answer")
    return address


def _read_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    scope: VinScope,
    deadline: float,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    # HTTPResponse.read1 returns the available bytes; read(n) can otherwise
    # trickle indefinitely while each individual socket read stays below timeout.
    reader = getattr(response, "read1", response.read)
    while size <= max_bytes:
        remaining = _remaining(scope, deadline)
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = reader(min(65_536, max_bytes + 1 - size))
        _remaining(scope, deadline)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise ValueError("document_too_large")


def _abort_connection(connection: http.client.HTTPConnection) -> None:
    # A socket timeout alone cannot bound trickled HTTP headers. Shutdown also
    # interrupts HTTPResponse's buffered reader when the total deadline expires.
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        except OSError:
            pass


def _rate_limit(origin: str, delay: float, scope: VinScope, deadline: float) -> None:
    remaining = _remaining(scope, deadline)
    with j1_fetch._RATE_LOCK:
        wait = max(0.0, j1_fetch._HOST_NEXT.get(origin, 0.0) - time.monotonic())
        if wait >= remaining:
            raise TimeoutError("fetch_timeout")
        j1_fetch._HOST_NEXT[origin] = time.monotonic() + wait + delay
    if wait:
        time.sleep(wait)
    _remaining(scope, deadline)


def _robots_policy(url: str, scope: VinScope, deadline: float) -> tuple[bool, float]:
    _remaining(scope, deadline)
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with j1_fetch._ROBOTS_LOCK:
        cached = j1_fetch._ROBOTS.get(origin)
    if cached and cached[0] > time.monotonic():
        parser, available, delay = cached[1:]
        return (available and (parser is None or parser.can_fetch(j1_fetch.USER_AGENT, url))), delay
    try:
        status, _, body, _ = _request_scoped(
            origin + "/robots.txt", scope, max_bytes=250_000, deadline=deadline, check_robots=False
        )
        if status == 404:
            policy: tuple[RobotFileParser | None, bool, float] = (None, True, 1.0)
        elif status == 200:
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", "replace").splitlines())
            crawl_delay = parser.crawl_delay(j1_fetch.USER_AGENT) or parser.crawl_delay("*") or 1.0
            policy = (parser, True, min(max(float(crawl_delay), 1.0), 30.0))
        else:
            policy = (None, False, 1.0)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(exc, TimeoutError) or str(exc) == "scope_expired":
            raise
        policy = (None, False, 1.0)
    with j1_fetch._ROBOTS_LOCK:
        j1_fetch._ROBOTS[origin] = (time.monotonic() + 3600, *policy)
    parser, available, delay = policy
    return (available and (parser is None or parser.can_fetch(j1_fetch.USER_AGENT, url))), delay


def _request_scoped(
    url: str,
    scope: VinScope,
    *,
    max_bytes: int,
    deadline: float,
    check_robots: bool,
) -> tuple[int, dict[str, str], bytes, str]:
    current = safe_url(url, scope)
    if not current:
        _require_active(scope)
        raise ValueError("unsafe_url")
    for hop in range(4):
        _remaining(scope, deadline)
        if max_bytes > j1_fetch.MAX_PDF_BYTES:
            classification = classify_source(current)
            if classification.source_tier not in {"A", "B"} or not classification.source_basis.startswith("registry:"):
                raise ValueError("large_document_source_untrusted")
        parsed = urlsplit(current)
        if check_robots:
            allowed, delay = _robots_policy(current, scope, deadline)
            if not allowed:
                raise ValueError("redirect_robots_disallowed" if hop else "robots_disallowed")
            _rate_limit(f"{parsed.scheme}://{parsed.netloc}", delay, scope, deadline)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = _resolve_public(parsed.hostname or "", port, scope, deadline)
        connection = (
            _PinnedHTTPS(parsed.hostname or "", address, port, _remaining(scope, deadline))
            if parsed.scheme == "https"
            else _PinnedHTTP(parsed.hostname or "", address, port, _remaining(scope, deadline))
        )
        watchdog = threading.Timer(_remaining(scope, deadline), _abort_connection, args=(connection,))
        watchdog.daemon = True
        watchdog.start()
        try:
            _remaining(scope, deadline)
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            connection.request(
                "GET",
                target,
                headers={
                    "User-Agent": j1_fetch.USER_AGENT,
                    "Accept": "text/html,application/pdf,text/plain",
                    "Accept-Encoding": "identity",
                },
            )
            remaining = _remaining(scope, deadline)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            _remaining(scope, deadline)
            headers = {name.casefold(): value for name, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                location = headers.get("location", "")
                next_url = safe_url(urljoin(current, location), scope) if location else ""
                if not next_url:
                    _require_active(scope)
                    raise ValueError("unsafe_redirect")
                if not check_robots and (
                    urlsplit(next_url).netloc != parsed.netloc or not j1_fetch.public_url(next_url)
                ):
                    raise ValueError("robots_redirected")
                current = next_url
                continue
            size_header = headers.get("content-length", "")
            if size_header.isdigit() and int(size_header) > max_bytes:
                raise ValueError("document_too_large")
            if headers.get("content-encoding", "identity").casefold() != "identity":
                raise ValueError("unsupported_content_encoding")
            body = _read_body(response, connection, scope, deadline, max_bytes)
            # Cookies, opaque identifiers and redirect URLs are not exported.
            selected_headers = {
                key: _clean_metadata(value, scope, 256)
                for key, value in headers.items()
                if key in {"content-type", "content-length", "content-encoding"}
            }
            _remaining(scope, deadline)
            return response.status, selected_headers, body, current
        finally:
            watchdog.cancel()
            connection.close()
    raise ValueError("too_many_redirects")


def request_vin(
    url: str, scope: VinScope, *, max_bytes: int, timeout: float = 10.0
) -> tuple[int, dict[str, str], bytes, str]:
    """GET a scoped public document with DNS pinning, robots and total deadline."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError("invalid_max_bytes")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 30
    ):
        raise ValueError("invalid_timeout")
    _require_active(scope)
    deadline = time.monotonic() + timeout
    try:
        return _request_scoped(url, scope, max_bytes=max_bytes, deadline=deadline, check_robots=True)
    except TimeoutError:
        raise TimeoutError("fetch_timeout") from None
    except (OSError, http.client.HTTPException):
        _require_active(scope)
        if time.monotonic() >= deadline:
            raise TimeoutError("fetch_timeout") from None
        raise OSError("fetch_failed") from None
    except ValueError as exc:
        allowed = {
            "scope_expired",
            "unsafe_url",
            "unsafe_redirect",
            "unsafe_dns_answer",
            "robots_disallowed",
            "redirect_robots_disallowed",
            "document_too_large",
            "unsupported_content_encoding",
            "too_many_redirects",
            "large_document_source_untrusted",
        }
        raise ValueError(str(exc) if str(exc) in allowed else "fetch_failed") from None


class _ProviderError(ValueError):
    def __init__(self, code: str, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _http_error(status: int) -> _ProviderError:
    code = {401: "http_unauthorized", 403: "http_forbidden", 429: "rate_limited"}.get(status)
    return _ProviderError(code or ("http_server_error" if status >= 500 else "http_error"), status)


def _searxng_search(query: str, scope: VinScope, engine: str) -> list[dict[str, Any]]:
    _require_active(scope)
    if engine not in {"bing", "yahoo"} or engine not in scope.allowed_engines or not safe_query(query, scope):
        raise ValueError("unsafe_query")
    base_url = os.environ.get("AUTOSTOP_J1_SEARXNG_URL", "")
    if not base_url:
        raise _ProviderError("provider_not_configured")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or j1_fetch.contains_sensitive(base_url)
    ):
        raise _ProviderError("searxng_url_invalid")
    deadline = time.monotonic() + 10
    target = (parsed.path.rstrip("/") or "") + "/search?q=" + quote_plus(query) + "&engines=" + engine + "&format=json"
    connection = _PinnedHTTP(
        parsed.hostname or "", parsed.hostname or "", parsed.port or 80, _remaining(scope, deadline)
    )
    watchdog = threading.Timer(_remaining(scope, deadline), _abort_connection, args=(connection,))
    watchdog.daemon = True
    watchdog.start()
    try:
        _remaining(scope, deadline)
        connection.request("GET", target, headers={"User-Agent": j1_fetch.USER_AGENT, "Accept": "application/json"})
        remaining = _remaining(scope, deadline)
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        _remaining(scope, deadline)
        if response.status != 200:
            raise _http_error(response.status)
        body = _read_body(response, connection, scope, deadline, MAX_SEARCH_BYTES)
        payload = json.loads(body)
    finally:
        watchdog.cancel()
        connection.close()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise _ProviderError("parse_failed")
    if payload.get("error") or payload.get("errors"):
        raise _ProviderError("engine_unavailable")
    if payload.get("unresponsive_engines") and not payload["results"]:
        raise _ProviderError("engine_unavailable")
    return [row for row in payload["results"][:30] if isinstance(row, dict)]


def _clean_metadata(value: object, scope: VinScope, limit: int) -> str:
    # Search metadata is text, not a serialized object with opaque credentials.
    if not isinstance(value, str):
        return ""
    plain = _decoded(value[: limit * 4])
    plain = "".join(char for char in plain if unicodedata.category(char) != "Cf")
    plain = re.sub(r"<[^>]*>", " ", plain)
    plain = " ".join(plain.split())
    marker = _MASK
    while marker in plain:
        marker += "_"
    masked = _mask_target(plain, scope, marker)
    clean = j1_fetch.redact_sensitive(masked, limit=limit)
    clean = _SECRET_FIELD.sub("[redacted]", clean)
    clean = _INTERNATIONAL_PHONE.sub("[redacted]", clean)
    return clean.replace(marker, scope.vin)[:limit]


class _DDGLinks(j1_fetch._DDGLinks):
    def __init__(self, scope: VinScope) -> None:
        super().__init__()
        self.scope = scope
        self._snippet_row: int | None = None
        self.has_result_links = False
        self.has_no_results = False
        self._result_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._snippet_row = None
            self._result_link = True
        if classes & {"no-results", "result--no-result"}:
            self.has_no_results = True
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            # Recognition precedes privacy filtering: a real results page can
            # contain only URLs that this VIN scope is forbidden to export.
            self.has_result_links |= self._result_link
            href = html.unescape(self._href)
            parsed = urlsplit(href)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
                href = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
            url = safe_url(href, self.scope)
            if url and len(self.rows) < j1_fetch.MAX_SEARCH_RESULTS:
                self.rows.append({"url": url, "title": " ".join(self._title), "snippet": "", "source": "duckduckgo"})
                self._snippet_row = len(self.rows) - 1
            self._href = ""
            self._result_link = False
        if self._snippet_tag and tag == self._snippet_tag:
            if self._snippet_row is not None:
                self.rows[self._snippet_row]["snippet"] = " ".join(self._snippet)
            self._snippet_tag = ""
            self._snippet = []


def _duckduckgo_search(query: str, scope: VinScope) -> list[dict[str, Any]]:
    if "duckduckgo" not in scope.allowed_engines or not safe_query(query, scope):
        raise ValueError("unsafe_query")
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    status, _, body, _ = request_vin(url, scope, max_bytes=MAX_SEARCH_BYTES)
    if status != 200:
        raise _http_error(status)
    text = body.decode("utf-8", "replace")
    if re.search(r"(?:anomaly-modal|id\s*=\s*[\"'](?:challenge-form|anomaly-form)[\"'])", text, re.I):
        raise _ProviderError("requires_human")
    parser = _DDGLinks(scope)
    parser.feed(text)
    parser.close()
    if not (parser.has_result_links or parser.has_no_results):
        raise _ProviderError("parse_failed")
    return parser.rows


def _rank_results(query: str, rows: list[dict[str, Any]], scope: VinScope) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        url = safe_url(str(row.get("url") or ""), scope)
        if not url:
            continue
        title = _clean_metadata(row.get("title"), scope, 200)
        snippet = _clean_metadata(row.get("content") or row.get("snippet") or row.get("description"), scope, 600)
        kind = "pdf" if urlsplit(url).path.casefold().endswith(".pdf") else ""
        classification = classify_source(url, title=title, kind=kind)
        key = j1_fetch._discovery_url_key(url)
        if key in merged:
            existing = merged[key]
            existing["engines"] = sorted({*existing["engines"], row["engine"]})
            if snippet and snippet not in existing["snippet"]:
                existing["snippet"] = (existing["snippet"] + " | " + snippet).strip(" |")[:600]
            continue
        merged[key] = {
            "url": url,
            "title": title,
            "snippet": snippet,
            "source": "duckduckgo" if row["engine"] == "duckduckgo" else "searxng",
            "engines": [row["engine"]],
            "source_class": classification.source_class,
            "source_tier": classification.source_tier,
            "source_basis": classification.source_basis,
        }
    ordered = sorted(merged.values(), key=lambda row: (-j1_fetch._discovery_score(query, row), row["url"]))
    results: list[dict[str, Any]] = []
    per_domain: dict[str, int] = {}
    for row in ordered:
        domain = discovery_domain(row["url"])
        if per_domain.get(domain, 0) >= j1_fetch.MAX_SEARCH_RESULTS_PER_DOMAIN:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        row["search_rank"] = len(results) + 1
        results.append(row)
        if len(results) >= j1_fetch.MAX_SEARCH_RESULTS:
            break
    return results


def _error_record(provider: str, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _ProviderError):
        row: dict[str, Any] = {"provider": provider, "error": exc.code}
        if exc.http_status is not None:
            row["http_status"] = exc.http_status
        return row
    if isinstance(exc, TimeoutError):
        code = "timeout"
    elif isinstance(exc, json.JSONDecodeError):
        code = "parse_failed"
    elif isinstance(exc, (OSError, http.client.HTTPException)):
        code = "transport_error"
    else:
        allowed = {"scope_expired", "unsafe_query", "robots_disallowed", "document_too_large", "unsafe_dns_answer"}
        code = str(exc) if str(exc) in allowed else "provider_error"
    return {"provider": provider, "error": code}


def search_vin(query: str, scope: VinScope) -> dict[str, Any]:
    """Search only selected engines; preserve empty/error provenance separately."""

    if not safe_query(query, scope):
        code = "scope_expired" if not _active(scope) else "unsafe_query"
        return {
            "ok": False,
            "result_class": "error",
            "results": [],
            "errors": [{"provider": "policy", "error": code}],
            "providers": [],
        }
    rows: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    successful = False
    stop_searxng = False
    for engine in (item for item in scope.allowed_engines if item != "duckduckgo"):
        if stop_searxng or not _active(scope):
            providers.append(
                {
                    "provider": engine,
                    "outcome": "skipped",
                    "reason": "scope_expired" if not _active(scope) else "provider_blocked",
                }
            )
            continue
        try:
            found = _searxng_search(query, scope, engine)
            successful = True
            rows.extend({**row, "engine": engine} for row in found)
            providers.append({"provider": engine, "outcome": "results" if found else "empty"})
        except (OSError, ValueError, http.client.HTTPException) as exc:
            error = _error_record(engine, exc)
            errors.append(error)
            providers.append({"provider": engine, "outcome": "error", **error})
            stop_searxng = error.get("http_status") in {401, 403, 429}
    ranked = _rank_results(query, rows, scope)
    if "duckduckgo" in scope.allowed_engines:
        if ranked or not _active(scope):
            providers.append(
                {
                    "provider": "duckduckgo",
                    "outcome": "skipped",
                    "reason": "primary_results" if ranked else "scope_expired",
                }
            )
        else:
            try:
                found = _duckduckgo_search(query, scope)
                successful = True
                rows.extend({**row, "engine": "duckduckgo"} for row in found)
                providers.append({"provider": "duckduckgo", "outcome": "results" if found else "empty"})
            except (OSError, ValueError, http.client.HTTPException) as exc:
                error = _error_record("duckduckgo", exc)
                errors.append(error)
                providers.append({"provider": "duckduckgo", "outcome": "error", **error})
            ranked = _rank_results(query, rows, scope)
    if not providers:
        errors.append({"provider": "policy", "error": "no_selected_engines"})
    if not _active(scope):
        ranked = []
        successful = False
        if not any(error["error"] == "scope_expired" for error in errors):
            errors.append({"provider": "policy", "error": "scope_expired"})
    return {
        "ok": bool(ranked) or successful,
        "result_class": "results" if ranked else ("empty" if successful else "error"),
        "results": ranked,
        "errors": errors,
        "providers": providers,
    }


__all__ = ["VinScope", "normalize_vin", "request_vin", "safe_query", "safe_url", "search_vin"]
