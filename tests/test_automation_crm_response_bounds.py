"""Bound the CRM adapter's owned buffer, independently of HTTPX decoding memory."""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from autostop_manager import automation_jobs
from autostop_manager.config import AutomationCrmConnectionConfig


READINESS = {
    "format": "crm_change_feed_readiness_v1",
    "consumer_id": automation_jobs.CRM_DIGEST_CONSUMER_ID,
    "consumer_registered": False,
    "pending_delivery": False,
    "pending_publish": False,
    "high_water": 0,
    "generation": "synthetic-generation",
}


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, *, read_error=None):
        self.chunks = chunks
        self.read_error = read_error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.read_error is not None:
            raise self.read_error

    async def aclose(self):
        self.closed = True


class Transport(httpx.MockTransport):
    closed = False

    async def aclose(self):
        await super().aclose()
        self.closed = True


class ResponseHarness:
    def __init__(self, monkeypatch, chunks, *, encoding=None, read_error=None):
        self.buffers = []
        observed_buffers = self.buffers

        class ObservedBuffer(bytearray):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.high_water = len(self)
                observed_buffers.append(self)

            def extend(self, value):
                super().extend(value)
                self.high_water = max(self.high_water, len(self))

        self.body = Body(chunks, read_error=read_error)
        self.clients = []
        self.responses = []

        def handle(_request):
            headers = {"Content-Encoding": encoding} if encoding else {}
            response = httpx.Response(200, headers=headers, stream=self.body)
            self.responses.append(response)
            return response

        self.transport = Transport(handle)
        real_client = httpx.AsyncClient

        def client(**kwargs):
            result = real_client(transport=self.transport, **kwargs)
            self.clients.append(result)
            return result

        monkeypatch.setattr(automation_jobs, "bytearray", ObservedBuffer, raising=False)
        monkeypatch.setattr(automation_jobs.httpx, "AsyncClient", client)
        self.source = automation_jobs.HttpCrmDigestSource(
            AutomationCrmConnectionConfig(
                configured=True,
                api_url="https://crm.invalid",
                bearer_token="synthetic-bearer",
            )
        )

    def assert_closed(self):
        assert len(self.responses) == len(self.clients) == 1
        assert self.body.closed
        assert self.responses[0].is_closed
        assert self.clients[0].is_closed
        assert self.transport.closed

    def assert_buffer_budget(self):
        # Require a real observed accumulator, including when an oversized
        # chunk is rejected before any bytes are copied into that accumulator.
        assert self.buffers
        assert max(buffer.high_water for buffer in self.buffers) <= automation_jobs.MAX_CRM_RESPONSE_BYTES


@pytest.mark.parametrize("scenario", ["single-large-chunk", "partial-then-large-chunk", "gzip-large-decoded-chunk"])
def test_oversized_decoded_response_bounds_owned_buffer_and_closes_resources(monkeypatch, scenario):
    maximum = automation_jobs.MAX_CRM_RESPONSE_BYTES
    encoding = None
    if scenario == "single-large-chunk":
        chunks = (b"x" * (maximum + 65536),)
    elif scenario == "partial-then-large-chunk":
        chunks = (b" " * (maximum - 1), b"x" * (maximum + 65536))
    else:
        chunks = (gzip.compress(b"x" * (maximum * 4)),)
        encoding = "gzip"
    harness = ResponseHarness(monkeypatch, chunks, encoding=encoding)

    with pytest.raises(automation_jobs.AutomationJobError, match=r"^automation_crm_response_too_large$") as raised:
        harness.source.readiness()

    assert raised.value.outcome_uncertain is False
    harness.assert_closed()
    harness.assert_buffer_budget()


def test_exact_cap_json_in_multiple_chunks_preserves_full_body_and_closes_resources(monkeypatch):
    encoded = json.dumps({"ok": True, "data": READINESS}).encode()
    body = b" " * (automation_jobs.MAX_CRM_RESPONSE_BYTES - len(encoded)) + encoded
    harness = ResponseHarness(monkeypatch, (body[:17], body[17:65536], body[65536:]))

    assert harness.source.readiness() == READINESS

    harness.assert_closed()
    harness.assert_buffer_budget()
    assert len(harness.buffers) == 1
    assert bytes(harness.buffers[0]) == body


@pytest.mark.parametrize("operation", ["readiness", "register", "ack"])
@pytest.mark.parametrize("failure", ["invalid-json", "read-error"])
def test_body_failure_preserves_error_and_mutation_uncertainty_and_closes_resources(monkeypatch, operation, failure):
    read_error = httpx.ReadError("synthetic stream read failure") if failure == "read-error" else None
    harness = ResponseHarness(monkeypatch, (b'{"ok":',), read_error=read_error)
    expected_code = "automation_crm_transport_failed" if read_error else "automation_crm_response_invalid"

    with pytest.raises(automation_jobs.AutomationJobError, match=f"^{expected_code}$") as raised:
        if operation == "ack":
            harness.source.acknowledge(ack="synthetic-ack")
        else:
            getattr(harness.source, operation)()

    assert raised.value.outcome_uncertain is (failure == "read-error" and operation in {"register", "ack"})
    harness.assert_closed()
    harness.assert_buffer_budget()
