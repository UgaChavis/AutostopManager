from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
from email.message import Message
from urllib.error import HTTPError, URLError

import httpx
import pytest
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream

from autostop_manager import bounded_http_read as bounded
from autostop_manager import vehicle_identity_transport as transport

SYNTHETIC_VIN = "1HG" + "CM8263" + "00000001"


def collect():
    return transport.collect_identity_provider_results(
        [{"ok": True, "identifier": SYNTHETIC_VIN, "identifier_type": "vin", "context": {}}],
        live_wmi=False,
        use_vpic_batch=False,
    )


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    for key, child in (
        ("AUTOSTOP_MANAGER_DB", "manager.sqlite3"),
        ("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", "runtime-unavailable"),
        ("AUTOSTOP_J1_CACHE_DIR", "j1"),
        ("AUTOSTOP_J1_VIN_CACHE_DIR", "vin"),
    ):
        monkeypatch.setenv(key, str(tmp_path / child))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("real network, provider, registry or subprocess forbidden")

    async def forbidden_fetch(*args, **kwargs):
        forbidden()

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(bounded, "_fetch", forbidden_fetch)
    for name in ("decode_vin_vpic", "decode_wmi_vpic", "load_source_registry"):
        monkeypatch.setattr(transport.lookup, name, forbidden)
    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())
    monkeypatch.setattr(transport, "_PROVIDER_SLOTS", threading.BoundedSemaphore(3))
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0)


@pytest.mark.parametrize(
    ("kind", "outcome"), [("busy", "provider_busy"), ("worker_start", "provider_executor_unavailable")]
)
@pytest.mark.parametrize("previous_failures", [0, 1])
def test_actual_local_rejection_preserves_provider_then_allows_healthy_request(
    monkeypatch, kind, outcome, previous_failures
):
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(bounded, "_ADMISSIONS", admission)
    circuit = transport._PROVIDER_CIRCUIT
    circuit.failures = previous_failures
    fetch_calls = []
    rejected_starts = []

    async def healthy_fetch(request, **kwargs):
        fetch_calls.append(1)
        body = json.dumps({"Results": [{"VIN": SYNTHETIC_VIN, "Make": "Synthetic"}]}).encode()
        return bounded.BufferedResponse(body, status=200, headers=Message(), url=request.full_url)

    monkeypatch.setattr(bounded, "_fetch", healthy_fetch)
    if kind == "busy":
        assert admission.acquire(blocking=False)
        try:
            result = collect()
        finally:
            admission.release()
    else:

        def rejected_start(thread):
            rejected_starts.append(1)
            raise RuntimeError("synthetic worker allocation rejected")

        with monkeypatch.context() as denied_worker:
            denied_worker.setattr(threading.Thread, "start", rejected_start)
            result = collect()
    row = result["vpic_results"][0]
    assert fetch_calls == []
    assert row["outcome"] == outcome
    assert row["retryable"] is False
    assert result["processing"]["retry_count"] == 0
    assert result["processing"]["http_attempts"] == 1
    assert circuit.failures == previous_failures
    assert circuit.blocked() is None
    assert len(rejected_starts) == (1 if kind == "worker_start" else 0)
    assert admission.acquire(blocking=False)
    admission.release()
    recovered = collect()
    assert recovered["vpic_results"][0]["ok"] is True
    assert recovered["processing"]["http_attempts"] == 1
    assert fetch_calls == [1]


@pytest.mark.parametrize("outcome", ["provider_busy", "provider_executor_unavailable"])
@pytest.mark.parametrize("previous_failures", [0, 1])
def test_local_capacity_receipt_does_not_record_provider_health(outcome, previous_failures):
    circuit = transport._PROVIDER_CIRCUIT
    circuit.failures = previous_failures
    transport._record({"outcome": outcome, "retryable": False, "ok": False})
    assert circuit.failures == previous_failures
    assert circuit.blocked() is None


@pytest.mark.parametrize("reason", [{"message": "synthetic network failure"}, ["synthetic network failure"]])
def test_unknown_urlerror_reason_retains_network_failure_contract(reason):
    result = transport._http_failure(URLError(reason))
    assert result["outcome"] == "network_error"
    assert result["retryable"] is True


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("reason", ["Service Unavailable", "provider_transport_busy", "provider_transport_unavailable"])
def test_remote_http_status_takes_priority_over_local_reason_and_records_health(status, reason):
    headers = Message()
    headers["Retry-After"] = "7"
    error = HTTPError("https://synthetic-provider.invalid/", status, reason, headers, None)

    result = transport._http_failure(error)

    assert result["outcome"] == ("provider_throttled" if status == 429 else "provider_http_5xx")
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 7
    circuit = transport._PROVIDER_CIRCUIT
    transport._record(result)
    assert circuit.failures == 1
    assert circuit.blocked() == "provider_throttled"


@pytest.mark.parametrize("decoder", [transport.lookup.decode_vin_vpic, transport.lookup.decode_wmi_vpic])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("reason", ["Service Unavailable", "provider_transport_busy", "provider_transport_unavailable"])
def test_public_decoder_remote_status_takes_priority_over_local_reason(monkeypatch, decoder, status, reason):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        headers = Message()
        headers["Retry-After"] = "7"
        raise HTTPError(request.full_url, status, reason, headers, None)

    monkeypatch.setattr(transport.lookup, "urlopen", fake_urlopen)
    identifier = SYNTHETIC_VIN if decoder.__name__ == "decode_vin_vpic" else SYNTHETIC_VIN[:3]

    result = decoder(identifier)

    assert len(calls) == 1
    assert identifier in calls[0].full_url
    assert result["request_url"] == calls[0].full_url
    assert result["ok"] is False
    assert result["outcome"] == ("provider_throttled" if status == 429 else "provider_http_5xx")
    assert result["retryable"] is True
    assert result["error"] == result["outcome"]
    assert reason not in result["error"]
    assert transport._PROVIDER_CIRCUIT.failures == 0


@pytest.mark.parametrize("decoder", [transport.lookup.decode_vin_vpic, transport.lookup.decode_wmi_vpic])
@pytest.mark.parametrize(
    ("reason", "outcome"),
    [("provider_transport_busy", "provider_busy"), ("provider_transport_unavailable", "provider_executor_unavailable")],
)
def test_public_decoder_true_local_rejection_remains_nonretryable(monkeypatch, decoder, reason, outcome):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise URLError(reason)

    monkeypatch.setattr(transport.lookup, "urlopen", fake_urlopen)
    identifier = SYNTHETIC_VIN if decoder.__name__ == "decode_vin_vpic" else SYNTHETIC_VIN[:3]

    result = decoder(identifier)

    assert len(calls) == 1
    assert result["ok"] is False
    assert result["outcome"] == outcome
    assert result["retryable"] is False
    assert result["error"] == outcome
    assert transport._PROVIDER_CIRCUIT.failures == 0


@pytest.mark.parametrize("decoder", [transport.lookup.decode_vin_vpic, transport.lookup.decode_wmi_vpic])
def test_public_decoder_success_preserves_exact_request_binding(monkeypatch, decoder):
    calls = []
    is_vin = decoder.__name__ == "decode_vin_vpic"
    identifier = SYNTHETIC_VIN if is_vin else SYNTHETIC_VIN[:3]
    row = {"VIN": identifier, "Make": "Synthetic"} if is_vin else {"WMI": identifier, "Name": "Synthetic"}
    body = json.dumps({"Results": [row]}).encode()

    def fake_urlopen(request, timeout):
        calls.append(request)
        return bounded.BufferedResponse(body, status=200, headers=Message(), url=request.full_url)

    monkeypatch.setattr(transport.lookup, "urlopen", fake_urlopen)

    result = decoder(identifier)

    assert len(calls) == 1
    assert result["ok"] is True
    assert result["request_url"] == calls[0].full_url
    assert result["vin" if is_vin else "wmi"] == identifier
    assert result["retryable"] is False
    assert transport._PROVIDER_CIRCUIT.failures == 0


@pytest.mark.parametrize("previous_failures", [0, 1])
def test_actual_async_pool_rejection_preserves_provider_and_independent_client(monkeypatch, previous_failures):
    async def run():
        first = SYNTHETIC_VIN
        second = "1HG" + "CM8263" + "00000002"
        inputs = [
            {"ok": True, "identifier": identifier, "identifier_type": "vin", "context": {}}
            for identifier in (first, second)
        ]
        body = json.dumps({"Results": [{"VIN": first, "Make": "Synthetic"}]}).encode()
        headers = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
        )
        release_body = asyncio.Event()
        capacity_observed = asyncio.Event()
        body_entered = asyncio.Event()
        local_errors = []
        connects = []
        circuit = transport._PROVIDER_CIRCUIT
        circuit.failures = previous_failures
        classifier = transport._http_failure

        class HeldStream(AsyncMockStream):
            def __init__(self):
                super().__init__([headers, body])
                self.reads = 0

            async def read(self, max_bytes, timeout=None):
                self.reads += 1
                if self.reads == 2:
                    body_entered.set()
                    await release_body.wait()
                return await super().read(max_bytes, timeout)

        class Backend(AsyncMockBackend):
            async def connect_tcp(self, *args, **kwargs):
                connects.append(1)
                return HeldStream()

        def capture_local_error(exc):
            result = classifier(exc)
            if isinstance(exc, httpx.PoolTimeout):
                local_errors.append(result)
                # A correct local classification completes on its first rejection.
                # Before the fix, wait until its retry/failure has finished too.
                if not result["retryable"] or previous_failures or len(local_errors) >= 2:
                    capacity_observed.set()
            return result

        monkeypatch.setattr(transport, "_http_failure", capture_local_error)
        wire = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        wire._pool._network_backend = Backend([])
        async with httpx.AsyncClient(transport=wire, trust_env=False) as client:
            task = asyncio.create_task(
                transport.collect_identity_provider_results_async(
                    inputs,
                    client=client,
                    live_wmi=False,
                    use_vpic_batch=False,
                    budget=transport.IdentityBudget(max_attempts=4, deadline_seconds=10),
                )
            )
            try:
                await asyncio.wait_for(body_entered.wait(), 1)
                await asyncio.wait_for(capacity_observed.wait(), 5)
                health_before_actual_response = (circuit.failures, circuit.blocked())
                release_body.set()
                result = await asyncio.wait_for(task, 1)
            finally:
                release_body.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert result["vpic_results"][0]["ok"] is True
        assert len(connects) == 1
        assert result["vpic_results"][1]["outcome"] == "provider_busy"
        assert result["vpic_results"][1]["retryable"] is False
        assert result["processing"]["http_attempts"] == 2
        assert result["processing"]["retry_count"] == 0
        assert health_before_actual_response == (previous_failures, None)
        following_calls = []

        def healthy_handler(request):
            following_calls.append(1)
            return httpx.Response(200, json={"Results": [{"VIN": second, "Make": "Synthetic"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(healthy_handler)) as client:
            following = await transport.collect_identity_provider_results_async(
                [inputs[1]],
                client=client,
                live_wmi=False,
                use_vpic_batch=False,
            )
        assert following["vpic_results"][0]["ok"] is True
        assert following_calls == [1]

    asyncio.run(run())
