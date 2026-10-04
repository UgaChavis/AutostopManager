from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import json
import socket
import time
from urllib.parse import parse_qs

import httpx
import pytest

from autostop_manager import vehicle_identity_transport as transport
from autostop_manager import vin_lookup
from autostop_manager.vehicle_identity_inputs import validate_identity_input


def synthetic_vin(index=1):
    return "1HG" + "CM8263" + f"{index:08d}"


def inputs(count):
    return [validate_identity_input(synthetic_vin(index)) for index in range(1, count + 1)]


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch):
    monkeypatch.setattr(transport, "_PROVIDER_CIRCUIT", transport._ProviderCircuit())

    def forbidden(*args, **kwargs):
        raise AssertionError("real network forbidden in E4 tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def request_rows(request):
    if request.method == "POST":
        data = parse_qs(request.content.decode())["data"][0]
        return [(row.split(",")[0], int(row.split(",")[1]) if "," in row else None) for row in data.split(";")]
    if "DecodeWMI" in request.url.path:
        return []
    return [
        (
            request.url.path.split("/")[-1],
            int(request.url.params["modelyear"]) if "modelyear" in request.url.params else None,
        )
    ]


def healthy_response(request):
    if "DecodeWMI" in request.url.path:
        return httpx.Response(200, json={"Results": [{"WMI": request.url.path.split("/")[-1], "Name": "Synthetic"}]})
    rows = request_rows(request)
    return httpx.Response(
        200,
        json={
            "Results": [
                {"VIN": vin, "Make": "Honda", "Model": f"Variant {year}", **({"ModelYear": str(year)} if year else {})}
                for vin, year in reversed(rows)
            ]
        },
    )


def run_async(items, handler, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await transport.collect_identity_provider_results_async(items, client=client, **kwargs)

    return asyncio.run(run())


@pytest.mark.parametrize("count, expected_chunks", [(0, 0), (1, 1), (50, 1), (51, 2), (500, 10)])
def test_async_chunks_preserve_all_positions_and_limit_provider_rows(count, expected_chunks):
    submitted = []

    def handler(request):
        submitted.append(len(request_rows(request)))
        return healthy_response(request)

    result = run_async(inputs(count), handler)
    assert submitted == [50] * (count // 50) + ([count % 50] if count % 50 else [])
    assert result["vpic_batch"]["chunk_count"] == expected_chunks
    assert len(result["vpic_results"]) == count
    assert all(row["ok"] for row in result["vpic_results"])
    assert result["processing"]["http_attempts"] == expected_chunks
    assert result["processing"]["partial"] is False


def test_async_deduplicates_equal_hints_and_isolates_different_hints():
    vin = synthetic_vin()
    items = [
        validate_identity_input(vin, model_year=2010),
        validate_identity_input(vin, model_year=2020),
        validate_identity_input(vin, model_year=2010),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return healthy_response(request)

    result = run_async(items, handler)
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert [row["vehicle"]["modelyear"] for row in result["vpic_results"]] == [2010, 2020, 2010]
    assert [row["model_year_hint_requested"] for row in result["vpic_results"]] == [2010, 2020, 2010]


def test_async_partial_and_frame_routing_uses_original_explicit_type():
    vin = synthetic_vin()
    items = [
        validate_identity_input(vin[:7] + "-" + vin[7:]),
        validate_identity_input(vin[:12]),
        validate_identity_input(vin[:12], identifier_type="vin_partial"),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return healthy_response(request)

    result = run_async(items, handler)
    assert len(calls) == 1
    assert result["vpic_results"][:2] == [None, None]
    assert result["vpic_results"][2]["identifier_binding"]["status"] == "compatible_partial"


def test_batch_attempt_cap_includes_primary_fallback_and_wmi():
    calls = []

    def handler(request):
        calls.append(request)
        return healthy_response(request)

    result = run_async(inputs(50), handler, use_vpic_batch=False)
    assert len(calls) == 20
    assert result["processing"]["http_attempts"] == 20
    assert result["processing"]["partial"] is True
    assert sum(row["ok"] for row in result["vpic_results"]) == 20
    assert all(row is not None for row in result["vpic_results"])


def test_timeout_retry_is_once_per_request_and_counts_against_single_cap(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive request url is not returned", request=request)

    budget = transport.IdentityBudget(max_attempts=2)
    result = run_async(inputs(1), handler, use_vpic_batch=False, budget=budget)
    assert len(calls) == 2
    assert result["processing"]["retry_count"] == 1
    assert result["processing"]["http_attempts"] == 2
    assert result["processing"]["provider_circuit_open"] is True
    assert "sensitive" not in json.dumps(result)


def test_a_transient_retry_can_recover_without_extra_wmi(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return healthy_response(request)

    result = run_async(inputs(1), handler, use_vpic_batch=False)
    assert result["vpic_results"][0]["ok"] is True
    assert result["processing"]["http_attempts"] == 2
    assert result["processing"]["retry_count"] == 1


@pytest.mark.parametrize("header_kind", ["seconds", "http_date"])
def test_retry_after_longer_than_deadline_returns_partial_without_fanout(header_kind):
    calls = []

    def handler(request):
        calls.append(request)
        # Compute dates when the provider responds: collection can precede this
        # test by several minutes in the complete suite.
        header = "60" if header_kind == "seconds" else format_datetime(datetime.now(UTC) + timedelta(minutes=2))
        return httpx.Response(429, headers={"Retry-After": header})

    result = run_async(inputs(1), handler, use_vpic_batch=False)
    assert len(calls) == 1
    assert result["vpic_results"][0]["outcome"] == "provider_throttled"
    assert result["processing"]["retry_count"] == 0
    assert result["processing"]["stop_reason"] == "provider_throttled"


def test_retry_after_is_waited_in_full_before_retry(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)
    times = []

    def handler(request):
        times.append(time.monotonic())
        if len(times) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return healthy_response(request)

    result = run_async(inputs(1), handler, use_vpic_batch=False)
    assert len(times) == 2
    assert times[1] - times[0] >= 1
    assert result["vpic_results"][0]["ok"] is True


def test_provider_failure_does_not_fan_out_500_rows(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    result = run_async(inputs(500), handler)
    assert len(calls) <= 4  # At most three in-flight primary chunks and the one request retry.
    assert result["processing"]["retry_count"] <= 1
    assert result["processing"]["partial"] is True
    assert len(result["vpic_results"]) == 500


def test_async_deadline_cancels_provider_attempt_and_stops_new_work():
    state = {"started": 0, "cancelled": 0}

    async def handler(request):
        state["started"] += 1
        try:
            await asyncio.sleep(10)
        finally:
            state["cancelled"] += 1
        return healthy_response(request)

    budget = transport.IdentityBudget(deadline_seconds=0.03)
    started = time.monotonic()
    result = run_async(inputs(51), handler, budget=budget)
    assert time.monotonic() - started < 1
    assert state["started"] == state["cancelled"] == 2
    assert result["processing"]["deadline_exceeded"] is True
    assert result["processing"]["http_attempts"] == 2


def test_async_request_deadline_keeps_provider_available_for_following_calls():
    async def healthy_but_delayed(request):
        await asyncio.sleep(1)
        return healthy_response(request)

    result = run_async(inputs(51), healthy_but_delayed, budget=transport.IdentityBudget(deadline_seconds=0.03))
    following = run_async(inputs(1), healthy_response)

    assert result["processing"]["deadline_exceeded"] is True
    assert result["processing"]["provider_circuit_open"] is False
    assert {row["outcome"] for row in result["vpic_results"]} == {"deadline_exceeded"}
    assert all(row["retryable"] is False for row in result["vpic_results"])
    assert result["processing"]["retry_count"] == 0
    assert following["vpic_results"][0]["ok"] is True
    assert following["processing"]["http_attempts"] == 1


def test_async_request_deadline_preserves_previous_provider_failure():
    transport._PROVIDER_CIRCUIT.record(transient=True, success=False)

    async def healthy_but_delayed(request):
        await asyncio.sleep(1)
        return healthy_response(request)

    result = run_async(inputs(51), healthy_but_delayed, budget=transport.IdentityBudget(deadline_seconds=0.03))
    assert result["processing"]["provider_circuit_open"] is False
    assert transport._PROVIDER_CIRCUIT.failures == 1

    def timed_out(request):
        raise httpx.ReadTimeout("synthetic provider timeout", request=request)

    following = run_async(inputs(1), timed_out, use_vpic_batch=False)
    assert following["processing"]["provider_circuit_open"] is True
    assert following["processing"]["http_attempts"] == 1


@pytest.mark.parametrize("httpx_timeout", [False, True])
def test_async_provider_timeout_before_short_request_deadline_still_opens_circuit(monkeypatch, httpx_timeout):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)

    def timed_out(request):
        if httpx_timeout:
            raise httpx.ReadTimeout("synthetic provider timeout", request=request)
        raise TimeoutError("synthetic provider timeout")

    budget = transport.IdentityBudget(max_attempts=2, deadline_seconds=1)
    result = run_async(inputs(1), timed_out, use_vpic_batch=False, budget=budget)
    assert result["vpic_results"][0]["outcome"] == "timeout"
    assert result["processing"]["deadline_exceeded"] is False
    assert result["processing"]["provider_circuit_open"] is True
    assert result["processing"]["http_attempts"] == 2
    assert result["processing"]["retry_count"] == 1


def test_async_cancellation_propagates_and_releases_global_slots():
    async def run():
        started = asyncio.Event()
        state = {"in_flight": 0, "cancelled": 0}

        async def handler(request):
            state["in_flight"] += 1
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                state["in_flight"] -= 1
                state["cancelled"] += 1
            return healthy_response(request)

        budget = transport.IdentityBudget()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                transport.collect_identity_provider_results_async(inputs(500), client=client, budget=budget)
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert state["in_flight"] == 0
        assert state["cancelled"] >= 1
        assert budget.active == 0
        assert budget.http_attempts <= 3

    asyncio.run(run())
    slots = [transport._PROVIDER_SLOTS.acquire(blocking=False) for _ in range(3)]
    assert all(slots)
    for _ in slots:
        transport._PROVIDER_SLOTS.release()


def test_process_concurrency_limit_and_event_loop_remain_responsive():
    async def run():
        active = 0
        peak = 0
        ticks = 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                return healthy_response(request)
            finally:
                active -= 1

        async def ticker():
            nonlocal ticks
            for _ in range(12):
                ticks += 1
                await asyncio.sleep(0.005)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(
                transport.collect_identity_provider_results_async(inputs(151), client=client),
                transport.collect_identity_provider_results_async(inputs(151), client=client),
                ticker(),
            )
        assert peak == 3
        assert ticks == 12

    asyncio.run(run())


def test_wmi_is_request_memoized_and_supports_low_volume_six_character_wmi():
    low_volume = "1G9" + "AA8263" + "AA123456"
    items = [
        validate_identity_input(low_volume),
        validate_identity_input(synthetic_vin()),
        validate_identity_input(synthetic_vin(2)),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return healthy_response(request)

    result = run_async(items, handler, live_vpic=False, live_wmi=True)
    assert len(calls) == 2
    assert len(result["wmi_results"][0]["wmi"]) == 6
    assert result["wmi_results"][1] is result["wmi_results"][2]


def test_empty_batch_row_falls_back_once_and_then_can_recover():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"Results": []})
        return healthy_response(request)

    result = run_async(inputs(2), handler)
    assert len(calls) == 3
    assert all(row["ok"] for row in result["vpic_results"])
    assert result["processing"]["retry_count"] == 0


def test_offline_flags_disable_both_providers():
    def forbidden(request):
        raise AssertionError("offline path attempted provider")

    result = run_async(inputs(5), forbidden, live_vpic=False, live_wmi=False)
    assert result["vpic_results"] == [None] * 5
    assert result["wmi_results"] == [None] * 5
    assert result["processing"]["http_attempts"] == 0


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        raw = json.dumps(self.payload).encode()
        return raw[:size] if size >= 0 else raw


def test_sync_51_rows_use_two_bounded_chunks(monkeypatch):
    counts = []

    def fake(request, timeout):
        rows = parse_qs(request.data.decode())["data"][0].split(";")
        counts.append(len(rows))
        assert timeout <= 8
        return FakeResponse({"Results": [{"VIN": row.split(",")[0], "Make": "Honda"} for row in rows]})

    monkeypatch.setattr(vin_lookup, "urlopen", fake)
    result = transport.collect_identity_provider_results(inputs(51))
    assert counts == [50, 1]
    assert result["processing"]["http_attempts"] == 2
    assert all(row["ok"] for row in result["vpic_results"])


def test_sync_outage_is_bounded_and_has_no_per_row_fanout(monkeypatch):
    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)
    calls = []

    def fake(request, timeout):
        calls.append(request)
        raise TimeoutError("raw identifiers and URL should not escape")

    monkeypatch.setattr(vin_lookup, "urlopen", fake)
    result = transport.collect_identity_provider_results(inputs(51))
    assert len(calls) == 2
    assert len(result["vpic_results"]) == 51
    assert result["processing"]["partial"] is True


@pytest.mark.parametrize("wrapped", [False, True])
def test_sync_request_deadline_does_not_change_provider_circuit(monkeypatch, wrapped):
    from urllib.error import URLError

    transport._PROVIDER_CIRCUIT.record(transient=True, success=False)
    budget = transport.IdentityBudget(max_attempts=2, deadline_seconds=1)

    def elapsed_request(request, timeout):
        assert timeout <= 1
        budget.started_at -= 1
        error = TimeoutError("synthetic request deadline")
        raise URLError(error) if wrapped else error

    monkeypatch.setattr(vin_lookup, "urlopen", elapsed_request)
    result = transport.collect_identity_provider_results(inputs(1), use_vpic_batch=False, budget=budget)
    assert result["vpic_results"][0]["outcome"] == "deadline_exceeded"
    assert result["vpic_results"][0]["retryable"] is False
    assert result["processing"]["provider_circuit_open"] is False
    assert result["processing"]["retry_count"] == 0
    assert transport._PROVIDER_CIRCUIT.failures == 1

    monkeypatch.setattr(
        vin_lookup,
        "urlopen",
        lambda request, timeout: FakeResponse({"Results": [{"VIN": synthetic_vin(), "Make": "Honda"}]}),
    )
    following = transport.collect_identity_provider_results(inputs(1), use_vpic_batch=False)
    assert following["vpic_results"][0]["ok"] is True
    assert following["processing"]["http_attempts"] == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_sync_provider_timeout_before_short_request_deadline_still_opens_circuit(monkeypatch, wrapped):
    from urllib.error import URLError

    monkeypatch.setattr(transport, "RETRY_BACKOFF_SECONDS", 0.0)

    def timed_out(request, timeout):
        error = TimeoutError("synthetic provider timeout")
        raise URLError(error) if wrapped else error

    monkeypatch.setattr(vin_lookup, "urlopen", timed_out)
    budget = transport.IdentityBudget(max_attempts=2, deadline_seconds=1)
    result = transport.collect_identity_provider_results(inputs(1), use_vpic_batch=False, budget=budget)
    assert result["vpic_results"][0]["outcome"] == ("network_error" if wrapped else "timeout")
    assert result["processing"]["deadline_exceeded"] is False
    assert result["processing"]["provider_circuit_open"] is True
    assert result["processing"]["http_attempts"] == 2
    assert result["processing"]["retry_count"] == 1


def test_501_items_are_rejected_without_provider_work():
    with pytest.raises(ValueError, match="identity_batch_too_large"):
        transport.collect_identity_provider_results(inputs(501))
