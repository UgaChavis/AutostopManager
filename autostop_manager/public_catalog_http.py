"""Bounded, anonymous public catalog reads with robots and pinned public DNS.

Each caller owns its request budget. DNS can outlive the caller deadline, so a
worker retains its admission until it actually ends; a timeout never claims to
have terminated OS name resolution. HTTP headers and decoded body share the
same monotonic deadline. No cookies or credentials are retained.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import AsyncIterator, Callable
import ipaddress
import re
import socket
import threading
import time
import zlib
from typing import Any, cast
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import anyio
import httpx


CATALOG_USER_AGENT = "AutoStopManagerCatalog/1.0"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
_ADMISSIONS = threading.BoundedSemaphore(2)
_ALLOWED_HOSTS = frozenset({"elcats.ru", "www.elcats.ru", "japancats.ru", "www.japancats.ru", "ssangyong.exist.ru"})


class CatalogReadError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CatalogPage:
    url: str
    text: str
    status: int


@dataclass(frozen=True)
class CatalogImage:
    url: str
    body: bytes
    status: int


def safe_catalog_url(url: str) -> str:
    """Only registered public origins; no identifier, credential or port input."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        unsafe = (
            parsed.scheme not in {"http", "https"}
            or host not in _ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}
            or "\\" in url
            or any(ord(char) < 32 for char in url)
            or len(url) > 4096
            # HTTPX removes literal dot segments after route/robots checks.
            # Reject encoded variants too, without rewriting opaque URL data.
            or any(segment in {".", ".."} for segment in unquote(parsed.path).replace("\\", "/").split("/"))
        )
        tokens = re.split(r"[?&=/]", unquote(parsed.path + "?" + parsed.query))
        private = any(
            re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", re.sub(r"[\s._\\/-]", "", token.upper())) for token in tokens
        ) or any(token.lower() in {"vin", "fin", "framenumber", "frame_number"} for token in tokens)
        if unsafe or private:
            raise CatalogReadError("unsafe_catalog_url")
        return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CatalogReadError("unsafe_catalog_url") from exc


def _public_address(host: str, port: int) -> str:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    except (OSError, ValueError) as exc:
        raise CatalogReadError("catalog_dns_failed", retryable=True) from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise CatalogReadError("unsafe_catalog_address")
    return str(addresses[0])


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogReadError("catalog_deadline_exceeded", retryable=True)
    return remaining


class _OwnedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        # HTTPX also closes at EOF; protect that first close before is_closed
        # can make later response.aclose calls skip incomplete stream cleanup.
        with anyio.CancelScope(shield=True):
            await self._stream.aclose()


async def _iter_catalog_response_bytes(
    response: httpx.Response, *, maximum: int, deadline: float
) -> AsyncIterator[bytes]:
    encodings = [value.strip().lower() for value in response.headers.get_list("content-encoding", split_commas=True)]
    # Buffered responses must retain HTTPX's existing decoded-content path.
    if response.is_stream_consumed or encodings != ["gzip"]:
        async for chunk in response.aiter_bytes():
            yield chunk
        return
    chunk_size = 16 * 1024
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
    emitted = 0

    def group(raw: bytes, room: int) -> bytes:
        kept = bytearray()

        def drain(data: bytes) -> None:
            while not decoder.eof:
                _remaining_time(deadline)
                try:
                    part = decoder.decompress(data, max_length=chunk_size)
                except zlib.error as error:
                    raise httpx.DecodingError(str(error), request=response.request) from error
                remaining = room - len(kept)
                if remaining > 0:
                    kept.extend(part[:remaining])
                data = decoder.unconsumed_tail
                if decoder.eof or (not data and len(part) < chunk_size):
                    return

        # Splitting input is internal only: the caller-visible transaction
        # boundary remains one original raw group, including CRC/error order.
        for offset in range(0, len(raw), chunk_size):
            drain(raw[offset : offset + chunk_size])
            if decoder.eof:
                break  # Match HTTPX's first-member behavior, ignoring trailing bytes.
        if not raw:
            drain(b"")
        return bytes(kept)

    async for raw in response.aiter_raw():
        part = group(raw, maximum + 1 - emitted)
        if part:
            yield part
            emitted += len(part)
    # HTTPX accepts missing trailers; do not add an EOF validity policy.
    # Capped empty-input drains avoid treating zlib.flush(length) as a cap.
    part = group(b"", maximum + 1 - emitted)
    if part:
        yield part


async def _async_http_get(
    url: str, *, address: str, deadline: float, maximum: int
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    pinned_host = f"[{address}]" if ":" in address else address
    pinned_url = urlunsplit((parsed.scheme, pinned_host, parsed.path, parsed.query, ""))
    headers = {"Host": host, "User-Agent": CATALOG_USER_AGENT, "Accept-Encoding": "identity"}
    deadline_scope = None
    try:
        remaining = _remaining_time(deadline)
        with anyio.fail_after(remaining) as deadline_scope:
            client = httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False)
            response = None
            try:
                await client.__aenter__()
                request = client.build_request("GET", pinned_url, headers=headers)
                request.extensions["sni_hostname"] = host
                # TLS/client setup can execute synchronously without an async checkpoint.
                _remaining_time(deadline)
                response = await client.send(request, stream=True, follow_redirects=False)
                response.stream = _OwnedResponseStream(cast(httpx.AsyncByteStream, response.stream))
                body = bytearray()
                # Navigation needs only headers; close intermediate bodies without buffering them.
                if response.status_code not in {301, 302, 303, 307, 308}:
                    async for chunk in _iter_catalog_response_bytes(response, maximum=maximum, deadline=deadline):
                        _remaining_time(deadline)
                        if len(body) + len(chunk) > maximum:
                            raise CatalogReadError("catalog_response_too_large")
                        body.extend(chunk)
                result = response.status_code, bytes(body), dict(response.headers)
            finally:
                # Caller timeout never releases capacity before the owned transport is closed.
                with anyio.CancelScope(shield=True):
                    try:
                        if response is not None:
                            await response.aclose()
                    finally:
                        await client.aclose()
            _remaining_time(deadline)
            return result
    except TimeoutError as exc:
        if deadline_scope is not None and deadline_scope.cancelled_caught:
            raise CatalogReadError("catalog_deadline_exceeded", retryable=True) from exc
        raise


def _http_get(
    url: str, timeout: float, maximum: int, *, deadline: float | None = None
) -> tuple[int, bytes, dict[str, str]]:
    deadline = time.monotonic() + timeout if deadline is None else deadline
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = 443 if parsed.scheme == "https" else 80
    _remaining_time(deadline)
    address = _public_address(host, port)
    _remaining_time(deadline)
    return asyncio.run(_async_http_get(url, address=address, deadline=deadline, maximum=maximum))


def _bounded_get(url: str, timeout: float, maximum: int) -> tuple[int, bytes, dict[str, str]]:
    deadline = time.monotonic() + timeout
    admission = _ADMISSIONS
    if not admission.acquire(blocking=False):
        raise CatalogReadError("catalog_transport_busy", retryable=True)
    done = threading.Event()
    outcome: list[tuple[int, bytes, dict[str, str]] | BaseException] = []

    def run() -> None:
        try:
            outcome.append(_http_get(url, timeout, maximum, deadline=deadline))
        except BaseException as exc:  # noqa: BLE001 - preserve worker failure for its caller.
            outcome.append(exc)
        finally:
            admission.release()
            done.set()

    try:
        threading.Thread(target=run, name="autostop-public-catalog", daemon=True).start()
    except RuntimeError as exc:
        admission.release()
        raise CatalogReadError("catalog_transport_unavailable", retryable=True) from exc
    if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        raise CatalogReadError("catalog_deadline_exceeded", retryable=True)
    value = outcome[0]
    if isinstance(value, CatalogReadError):
        raise value
    if isinstance(value, (httpx.TimeoutException, TimeoutError)):
        raise CatalogReadError("catalog_timeout", retryable=True) from value
    if isinstance(value, (httpx.HTTPError, OSError)):
        raise CatalogReadError("catalog_transport_failed", retryable=True) from value
    if isinstance(value, BaseException):
        raise CatalogReadError("catalog_transport_failed") from value
    return value


def _decode_html(body: bytes, headers: dict[str, str]) -> str:
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", headers.get("content-type", ""), re.I)
    meta = re.search(rb"charset\s*=\s*[\"']?([\w-]+)", body[:4096], re.I)
    charset = declared.group(1) if declared else meta.group(1).decode("ascii") if meta else None
    try:
        if charset:
            return body.decode("utf-8-sig" if charset.lower().replace("_", "-") == "utf-8" else charset)
        try:
            return body.decode("utf-8-sig")
        except UnicodeDecodeError:
            return body.decode("cp1251")
    except (LookupError, UnicodeError) as exc:
        raise CatalogReadError("catalog_encoding_error") from exc


class PublicCatalogReader:
    def __init__(self, *, page_budget: int = 12, deadline_seconds: float = 45) -> None:
        self.page_budget = page_budget
        self.deadline = time.monotonic() + deadline_seconds
        self.network_calls = 0
        self.attempts: list[dict[str, Any]] = []
        self._robots: dict[str, RobotFileParser] = {}
        self._pages: dict[str, CatalogPage] = {}

    def _request(self, url: str, maximum: int) -> tuple[int, bytes, dict[str, str]]:
        if self.network_calls >= self.page_budget:
            raise CatalogReadError("catalog_page_budget_exceeded")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogReadError("catalog_deadline_exceeded")
        self.network_calls += 1
        started = time.monotonic()
        attempt: dict[str, Any] = {"url": url, "method": "GET"}
        self.attempts.append(attempt)
        try:
            status, body, headers = _bounded_get(url, min(12, remaining), maximum)
            attempt["status"] = status
            return status, body, headers
        except CatalogReadError as exc:
            attempt["error"] = exc.code
            raise
        finally:
            attempt["elapsed_ms"] = round((time.monotonic() - started) * 1000)

    def _fetch(
        self,
        url: str,
        *,
        robots: bool = False,
        maximum: int = MAX_CATALOG_BYTES,
        route_guard: Callable[[str], str] | None = None,
    ) -> tuple[str, int, bytes, dict[str, str]]:
        current = safe_catalog_url(url)
        for _ in range(4):
            if route_guard is not None:
                current = route_guard(current)
            if not robots:
                self._require_robots(current)
            status, body, headers = self._request(current, 262144 if robots else maximum)
            if status in {301, 302, 303, 307, 308}:
                destination = safe_catalog_url(urljoin(current, headers.get("location", "")))
                if urlsplit(current).hostname != urlsplit(destination).hostname:
                    raise CatalogReadError("unsafe_catalog_redirect")
                if urlsplit(current).scheme == "https" and urlsplit(destination).scheme != "https":
                    raise CatalogReadError("unsafe_catalog_redirect")
                current = destination
                continue
            return current, status, body, headers
        raise CatalogReadError("catalog_redirect_limit")

    def _require_robots(self, url: str) -> None:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if origin not in self._robots:
            robot_url = origin + "/robots.txt"
            _, status, body, _ = self._fetch(robot_url, robots=True)
            policy = RobotFileParser(robot_url)
            if status in {404, 410}:
                policy.parse([])  # No published robots policy, per the HTTP robots convention.
            elif status in {401, 403}:
                raise CatalogReadError("robots_disallowed")
            elif status == 200:
                try:
                    text = body.decode("utf-8-sig")
                except UnicodeError as exc:
                    raise CatalogReadError("robots_unavailable") from exc
                if re.search(r"<\s*(?:html|body|script|!doctype)", text, re.I):
                    raise CatalogReadError("robots_unavailable")
                policy.parse(text.splitlines())
            else:
                raise CatalogReadError("robots_unavailable", retryable=status >= 500)
            self._robots[origin] = policy
        if not self._robots[origin].can_fetch(CATALOG_USER_AGENT, url):
            raise CatalogReadError("robots_disallowed")

    def read(self, url: str, *, route_guard: Callable[[str], str] | None = None) -> CatalogPage:
        safe = safe_catalog_url(url)
        if route_guard is not None:
            safe = route_guard(safe)
        if safe in self._pages:
            return self._pages[safe]
        final, status, body, headers = self._fetch(safe, route_guard=route_guard)
        if status in {401, 403}:
            raise CatalogReadError("catalog_auth_required")
        if status == 429:
            raise CatalogReadError("catalog_rate_limited")
        if status >= 500:
            raise CatalogReadError("catalog_provider_error", retryable=True)
        if status != 200:
            raise CatalogReadError("catalog_http_error")
        content_type = headers.get("content-type", "text/html").lower()
        if "html" not in content_type and "text/plain" not in content_type:
            raise CatalogReadError("catalog_content_type_error")
        page = CatalogPage(final, _decode_html(body, headers), status)
        self._pages[safe] = page
        return page

    def read_number_image(self, url: str) -> CatalogImage:
        """Read only the public part-number image endpoint, inside the same budget."""
        safe = safe_catalog_url(url)
        parsed = urlsplit(safe)
        if parsed.hostname != "ssangyong.exist.ru" or parsed.path.casefold() != "/pcode.ashx":
            raise CatalogReadError("unsupported_number_image")
        final, status, body, _ = self._fetch(safe, maximum=131072)
        if urlsplit(final).path.casefold() != "/pcode.ashx" or urlsplit(final).hostname != parsed.hostname:
            raise CatalogReadError("unsupported_number_image")
        if status != 200:
            raise CatalogReadError("catalog_number_image_unavailable")
        # The public endpoint currently labels a PNG as JPEG. Validate its actual
        # signature and bounded dimensions before invoking the local decoder.
        if len(body) < 24 or body[:8] != b"\x89PNG\r\n\x1a\n" or body[12:16] != b"IHDR":
            raise CatalogReadError("unsupported_number_image")
        width = int.from_bytes(body[16:20], "big")
        height = int.from_bytes(body[20:24], "big")
        if not (1 <= width <= 2048 and 1 <= height <= 256):
            raise CatalogReadError("unsupported_number_image")
        return CatalogImage(final, body, status)
