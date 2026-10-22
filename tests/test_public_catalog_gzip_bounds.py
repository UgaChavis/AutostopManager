"""Bound single-gzip decoding while preserving HTTPX response semantics."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import time
import zlib

import httpx
import pytest

from autostop_manager import public_catalog_http as catalog


URL = "https://www.elcats.ru/vw/"
# Valid raw deflate whose first bytes also look like zlib, wrapped in gzip.
# Splitting intermediate decoder calls used to break HTTPX's first-call fallback.
CHAIN = bytes.fromhex(
    "1f8b0800000000000203edc1410100201003a019cd2c9630cc759d357c0077723ad9df5a6d52000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000681f6b4cfa7da5000100"
)


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def read(monkeypatch, response, maximum):
    clients = []
    body = response.stream
    real_client = httpx.AsyncClient

    def handle(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["Host"] == "www.elcats.ru"
        assert request.extensions["sni_hostname"] == "www.elcats.ru"
        return response

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        result = real_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(catalog.httpx, "AsyncClient", client)
    try:
        return asyncio.run(
            catalog._async_http_get(URL, address="8.8.8.8", deadline=time.monotonic() + 10, maximum=maximum)
        )
    finally:
        assert response.is_closed and clients and all(item.is_closed for item in clients)
        if isinstance(body, Body):
            assert body.closed


def test_gzip_size_rejection_does_not_allocate_full_decoded_body(monkeypatch):
    encoded = gzip.compress(b"x" * (8 * 1024 * 1024), mtime=0)
    maximum = 128 * 1024
    output_sizes = []
    real_decoder = zlib.decompressobj

    class ObservedDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = real_decoder(*args, **kwargs)

        def decompress(self, *args, **kwargs):
            result = self.decoder.decompress(*args, **kwargs)
            output_sizes.append(len(result))
            return result

        def flush(self, *args, **kwargs):
            result = self.decoder.flush(*args, **kwargs)
            output_sizes.append(len(result))
            return result

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", ObservedDecoder)
    response = httpx.Response(200, headers={"Content-Encoding": " GZiP "}, stream=Body([encoded]))
    with pytest.raises(catalog.CatalogReadError, match=r"^catalog_response_too_large$") as error:
        read(monkeypatch, response, maximum)
    assert error.value.retryable is False
    assert output_sizes and max(output_sizes) <= maximum + 1


@pytest.mark.parametrize("overflow", [False, True])
def test_gzip_decoded_cap_is_inclusive_across_wire_chunks(monkeypatch, overflow):
    payload = b"x" * (16 * 1024 + 3)
    encoded = gzip.compress(payload, mtime=0)
    body = Body([encoded[offset : offset + 7] for offset in range(0, len(encoded), 7)])
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)
    if overflow:
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_response_too_large$"):
            read(monkeypatch, response, len(payload) - 1)
    else:
        assert read(monkeypatch, response, len(payload))[:2] == (200, payload)


@pytest.mark.parametrize("same_chunk", [False, True])
def test_gzip_crc_precedence_keeps_original_wire_chunk_boundary(monkeypatch, same_chunk):
    encoded = gzip.compress(b"x" * (256 * 1024), mtime=0)
    bad_crc = encoded[:-8] + bytes([encoded[-8] ^ 0xFF]) + encoded[-7:]
    body = Body([bad_crc] if same_chunk else [bad_crc[:-8], bad_crc[-8:]])
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)
    if same_chunk:
        with pytest.raises(httpx.DecodingError):
            read(monkeypatch, response, 16 * 1024)
    else:
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_response_too_large$"):
            read(monkeypatch, response, 16 * 1024)
    assert body.yielded == 1


@pytest.mark.parametrize("kind", ["missing-trailer", "first-member-with-tail", "empty-wire"])
def test_gzip_eof_keeps_httpx_first_member_and_missing_trailer_behavior(monkeypatch, kind):
    payload = b"first member"
    encoded = gzip.compress(payload, mtime=0)
    if kind == "missing-trailer":
        chunks, expected = [encoded[:-8]], payload
    elif kind == "first-member-with-tail":
        chunks, expected = [encoded, gzip.compress(b"second member", mtime=0) + b"junk"], payload
    else:
        chunks, expected = [], b""
    body = Body(chunks)
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)
    assert read(monkeypatch, response, len(payload))[:2] == (200, expected)
    assert body.yielded == len(chunks)


def test_buffered_gzip_response_uses_its_already_decoded_content(monkeypatch):
    payload = b"already decoded"
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(payload, mtime=0))
    assert response.is_stream_consumed
    assert read(monkeypatch, response, len(payload))[:2] == (200, payload)


def test_valid_encoding_chain_keeps_httpx_deflate_fallback(monkeypatch):
    response = httpx.Response(
        200, headers=[("Content-Encoding", "deflate"), ("Content-Encoding", "gzip")], stream=Body([CHAIN])
    )
    status, decoded, _headers = read(monkeypatch, response, 100000)
    assert status == 200 and len(decoded) == 65691
    assert hashlib.sha256(decoded).hexdigest() == "118f234f91a8a49b4a165f3f6a41ff85c1c185358ba4dacb9ff575690f924da6"
