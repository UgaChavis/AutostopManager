"""Bounded vPIC collection shared by sync library and asynchronous E4 tools.

Only this module schedules provider work. Parsing and identity merging remain pure.
Diagnostics contain counters and error codes, never identifiers or request URLs.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import threading
import time
import math
from typing import Any, Literal
from urllib.error import HTTPError, URLError

import httpx

from . import vin_lookup as lookup

DEFAULT_DEADLINE_SECONDS = 30.0
MAX_IDENTITY_ITEMS = 500
VPIC_CHUNK_SIZE = 50
MAX_PROVIDER_CONCURRENCY = 3
SINGLE_HTTP_ATTEMPT_CAP = 2
BATCH_HTTP_ATTEMPT_CAP = 20
RETRY_BACKOFF_SECONDS = 0.5
CIRCUIT_COOLDOWN_SECONDS = 30.0
HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=1.0)
_PROVIDER_SLOTS = threading.BoundedSemaphore(MAX_PROVIDER_CONCURRENCY)


class _ProviderCircuit:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures = 0
        self.open_until = 0.0
        self.throttled_until = 0.0

    def blocked(self) -> str | None:
        with self.lock:
            now = time.monotonic()
            if self.open_until > now:
                return "provider_circuit_open"
            if self.throttled_until > now:
                return "provider_throttled"
            return None

    def record(self, *, transient: bool, success: bool, retry_after: float | None = None) -> None:
        with self.lock:
            now = time.monotonic()
            if transient:
                self.failures += 1
                if retry_after is not None:
                    self.throttled_until = max(self.throttled_until, now + retry_after)
                if self.failures >= 2:
                    self.open_until = now + CIRCUIT_COOLDOWN_SECONDS
                    self.failures = 0
            elif success and self.open_until <= now:
                self.failures = 0


_PROVIDER_CIRCUIT = _ProviderCircuit()


@dataclass
class IdentityBudget:
    """A monotonic work budget, not a promise to decode all 500 rows within 30 s."""

    max_attempts: int = BATCH_HTTP_ATTEMPT_CAP
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    http_attempts: int = 0
    retry_count: int = 0
    retry_reserved: bool = False
    stop_code: str | None = None
    peak_concurrency: int = 0
    active: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.started_at + self.deadline_seconds - time.monotonic())

    def reason(self) -> str | None:
        if self.stop_code:
            return self.stop_code
        if self.remaining <= 0:
            return "deadline_exceeded"
        if self.http_attempts >= self.max_attempts:
            return "attempt_budget_exhausted"
        return None

    def begin(self) -> str | None:
        with self.lock:
            reason = self.reason()
            if reason:
                return reason
            self.http_attempts += 1
            self.active += 1
            self.peak_concurrency = max(self.peak_concurrency, self.active)
            return None

    def finish(self) -> None:
        with self.lock:
            self.active -= 1

    def reserve_retry(self, delay: float) -> bool:
        with self.lock:
            if self.retry_reserved or self.reason() or self.remaining <= delay + 0.05:
                return False
            self.retry_reserved = True
            return True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "deadline_seconds": self.deadline_seconds,
            "max_http_attempts": self.max_attempts,
            "http_attempts": self.http_attempts,
            "retry_count": self.retry_count,
            "retry_scheduled": self.retry_reserved,
            "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 2),
            "deadline_exceeded": self.remaining <= 0,
            "attempt_budget_exhausted": self.http_attempts >= self.max_attempts,
            "stop_reason": self.stop_code,
            "peak_concurrency": self.peak_concurrency,
            "provider_circuit_open": _PROVIDER_CIRCUIT.blocked() == "provider_circuit_open",
        }


RequestKey = tuple[str, int | None]


@dataclass(frozen=True)
class _Job:
    kind: Literal["vin", "batch", "wmi"]
    keys: tuple[RequestKey, ...]

    def request(self) -> tuple[str, bytes | None]:
        if self.kind == "batch":
            return lookup._batch_request(list(self.keys))
        if self.kind == "wmi":
            return lookup._wmi_request(self.keys[0][0]), None
        vin, year = self.keys[0]
        return lookup._vin_request(vin, model_year=year), None

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.kind == "batch":
            return lookup._parse_batch_payload(payload, list(self.keys))
        if self.kind == "wmi":
            return lookup._parse_wmi_payload(payload, self.keys[0][0])
        vin, _year = self.keys[0]
        return lookup._parse_vin_payload(payload, vin, partial=len(vin) < 17 or "*" in vin)


@dataclass
class _Collection:
    inputs: list[dict[str, Any]]
    budget: IdentityBudget
    groups: dict[RequestKey, list[int]] = field(default_factory=dict)
    results: dict[RequestKey, dict[str, Any]] = field(default_factory=dict)
    wmi_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    primary_jobs: list[_Job] = field(default_factory=list)
    batch_attempted: bool = False
    batch_chunks: int = 0

    def prepare(self, *, use_vpic_batch: bool) -> None:
        if len(self.inputs) > MAX_IDENTITY_ITEMS:
            raise ValueError("identity_batch_too_large")
        for index, item in enumerate(self.inputs):
            if not item.get("ok", True):
                continue
            classification = lookup.classify_identifier(
                item.get("identifier", ""), identifier_type=item.get("identifier_type", "auto")
            )
            if classification.kind not in {"vin", "vin_partial"}:
                continue
            context = item.get("context") or {}
            year = context.get("model_year", item.get("model_year"))
            try:
                if year is not None:
                    lookup._provider_year(year)
            except ValueError:
                continue
            self.groups.setdefault((classification.normalized, year), []).append(index)
        variants: dict[str, set[int | None]] = defaultdict(set)
        for vin, year in self.groups:
            variants[vin].add(year)
        batch_keys = [key for key in self.groups if use_vpic_batch and len(variants[key[0]]) == 1]
        single_keys = [key for key in self.groups if key not in batch_keys]
        self.primary_jobs = [
            _Job("batch", tuple(batch_keys[index : index + VPIC_CHUNK_SIZE]))
            for index in range(0, len(batch_keys), VPIC_CHUNK_SIZE)
        ]
        self.primary_jobs.extend(_Job("vin", (key,)) for key in single_keys)

    def accept(self, job: _Job, result: dict[str, Any]) -> None:
        if job.kind == "wmi":
            self.wmi_results[job.keys[0][0]] = result
            return
        if job.kind == "batch":
            if result.get("http_attempted"):
                self.batch_attempted = True
                self.batch_chunks += 1
            by_vin = result.get("results_by_vin")
            for key in job.keys:
                row = by_vin.get(key[0]) if isinstance(by_vin, dict) else None
                if not isinstance(row, dict):
                    row = {**result, "vehicle": {}}
                    row.pop("results_by_vin", None)
                self.results[key] = self._hint(row, key[1])
        else:
            self.results[job.keys[0]] = self._hint(result, job.keys[0][1])

    @staticmethod
    def _hint(result: dict[str, Any], year: int | None) -> dict[str, Any]:
        if year is None:
            return result
        return {**result, "model_year_hint_requested": year}

    def fallback_jobs(self) -> list[_Job]:
        # A transient outage, throttling or budget stop must not fan out to every row.
        return [
            _Job("vin", (key,))
            for key, result in self.results.items()
            if not result.get("ok")
            and result.get("outcome")
            in {"empty_result", "adapter_malformed_payload", "ambiguous_provider_result", "identity_unverified"}
            and result.get("source") == "NHTSA vPIC Batch"
        ]

    def wmi_jobs(self) -> list[_Job]:
        keys: dict[str, None] = {}
        for vin, year in self.groups:
            result = self.results.get((vin, year)) or {}
            vehicle = result.get("vehicle") or {}
            if vehicle.get("make"):
                continue
            # Low-volume WMIs use VIN positions 1-3 and 12-14; never truncate a 6-character WMI.
            wmi = wmi_for_vin(vin)
            if "*" not in wmi:
                keys[wmi] = None
        return [_Job("wmi", ((wmi, None),)) for wmi in keys]

    def result(self, *, live_vpic: bool, live_wmi: bool) -> dict[str, Any]:
        vpic_rows: list[dict[str, Any] | None] = [None] * len(self.inputs)
        wmi_rows: list[dict[str, Any] | None] = [None] * len(self.inputs)
        for key, indices in self.groups.items():
            vin, _year = key
            wmi = wmi_for_vin(vin)
            for index in indices:
                if live_vpic:
                    vpic_rows[index] = self.results.get(key) or _stopped(self.budget)
                if live_wmi:
                    wmi_rows[index] = self.wmi_results.get(wmi)
        valid = [row for row in vpic_rows if row is not None]
        processing = self.budget.diagnostics()
        processing["partial"] = any(not row.get("ok") for row in valid) or any(
            row is not None and not row.get("ok") for row in wmi_rows
        )
        if processing["partial"] and processing["stop_reason"] is None:
            processing["stop_reason"] = self.budget.reason() or _PROVIDER_CIRCUIT.blocked()
        return {
            "vpic_results": vpic_rows,
            "wmi_results": wmi_rows,
            "vpic_batch": {
                "attempted": self.batch_attempted,
                "ok": all(row.get("ok") for row in valid),
                "decoded_count": sum(row.get("ok", False) for row in self.results.values()),
                "chunk_count": self.batch_chunks,
                "error": next((row.get("outcome") for row in valid if not row.get("ok")), None),
            },
            "processing": processing,
        }


def _stopped(budget: IdentityBudget) -> dict[str, Any]:
    reason = budget.reason() or _PROVIDER_CIRCUIT.blocked() or "provider_not_completed"
    return lookup._provider_failure(reason)


def wmi_for_vin(vin: str) -> str:
    """The six-character low-volume key combines positions 1-3 and 12-14."""
    return vin[:3] + vin[11:14] if len(vin) == 17 and vin[2:3] == "9" else vin[:3]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            delay = float(value.strip())
            return delay if math.isfinite(delay) else None
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, (date - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _http_failure(exc: BaseException) -> dict[str, Any]:
    retry_after: float | None = None
    if isinstance(exc, (HTTPError, httpx.HTTPStatusError)):
        status = int(exc.code) if isinstance(exc, HTTPError) else exc.response.status_code
        headers = exc.headers if isinstance(exc, HTTPError) else exc.response.headers
        if status in {429, 503}:
            retry_after = _retry_after(headers.get("Retry-After")) if headers else None
        if status == 429:
            outcome, retryable = "provider_throttled", True
        else:
            outcome, retryable = ("provider_http_5xx", True) if status >= 500 else ("provider_http_4xx", False)
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        outcome, retryable = "timeout", True
    elif isinstance(exc, (URLError, OSError, httpx.TransportError)):
        outcome, retryable = "network_error", True
    else:
        outcome, retryable = "adapter_malformed_payload", False
    error = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("vPIC returned a ") else None
    result = lookup._provider_failure(outcome, retryable=retryable, error=error)
    if retry_after is not None:
        result["retry_after_seconds"] = retry_after
    return result


def _record(result: dict[str, Any]) -> None:
    _PROVIDER_CIRCUIT.record(
        transient=bool(result.get("retryable")),
        success=not result.get("retryable", False),
        retry_after=result.get("retry_after_seconds"),
    )


def _retry_delay(result: dict[str, Any], budget: IdentityBudget) -> float | None:
    if not result.get("retryable"):
        return None
    delay = max(RETRY_BACKOFF_SECONDS, result.get("retry_after_seconds", 0.0))
    if budget.reserve_retry(delay):
        return delay
    if result.get("outcome") == "provider_throttled":
        budget.stop_code = "provider_throttled"
    return None


def _run_sync(job: _Job, budget: IdentityBudget) -> dict[str, Any]:
    retrying = False
    while True:
        reason = budget.reason() or _PROVIDER_CIRCUIT.blocked()
        if reason:
            return lookup._provider_failure(reason)
        if not _PROVIDER_SLOTS.acquire(timeout=budget.remaining):
            return lookup._provider_failure("deadline_exceeded")
        begun = False
        try:
            reason = budget.reason() or _PROVIDER_CIRCUIT.blocked() or budget.begin()
            if reason:
                return lookup._provider_failure(reason)
            begun = True
            if retrying:
                budget.retry_count += 1
            url, data = job.request()
            result = job.parse(lookup._vpic_request_json(url, timeout=min(8.0, budget.remaining), data=data))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            result = _http_failure(exc)
        finally:
            if begun:
                budget.finish()
            _PROVIDER_SLOTS.release()
        result["http_attempted"] = True
        result["source"] = (
            "NHTSA vPIC WMI" if job.kind == "wmi" else ("NHTSA vPIC Batch" if job.kind == "batch" else "NHTSA vPIC")
        )
        _record(result)
        delay = _retry_delay(result, budget)
        if delay is None:
            return result
        retrying = True
        time.sleep(delay)


async def _request_async(client: httpx.AsyncClient, job: _Job, budget: IdentityBudget) -> dict[str, Any]:
    url, data = job.request()
    headers = {"User-Agent": "AutostopManager/0.1"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    # Streaming enforces the same response size bound as urllib and closes on cancellation.
    async with client.stream(
        "POST" if data is not None else "GET", url, content=data, headers=headers, timeout=HTTP_TIMEOUT
    ) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > lookup.MAX_VPIC_RESPONSE_BYTES:
                raise ValueError("provider_response_too_large")
        import json

        payload = lookup._validate_vpic_payload(json.loads(raw.decode("utf-8")))
        return job.parse(payload)


async def _run_async(job: _Job, budget: IdentityBudget, client: httpx.AsyncClient) -> dict[str, Any]:
    retrying = False
    while True:
        reason = budget.reason() or _PROVIDER_CIRCUIT.blocked()
        if reason:
            return lookup._provider_failure(reason)
        acquired = False
        begun = False
        try:
            while not _PROVIDER_SLOTS.acquire(blocking=False):
                if budget.reason():
                    return _stopped(budget)
                await asyncio.sleep(min(0.01, budget.remaining))
            acquired = True
            reason = budget.reason() or _PROVIDER_CIRCUIT.blocked() or budget.begin()
            if reason:
                return lookup._provider_failure(reason)
            begun = True
            if retrying:
                budget.retry_count += 1
            async with asyncio.timeout(min(8.0, budget.remaining)):
                result = await _request_async(client, job, budget)
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            result = _http_failure(exc)
        finally:
            if begun:
                budget.finish()
            if acquired:
                _PROVIDER_SLOTS.release()
        result["http_attempted"] = True
        result["source"] = (
            "NHTSA vPIC WMI" if job.kind == "wmi" else ("NHTSA vPIC Batch" if job.kind == "batch" else "NHTSA vPIC")
        )
        _record(result)
        delay = _retry_delay(result, budget)
        if delay is None:
            return result
        retrying = True
        await asyncio.sleep(delay)


async def _stage_async(jobs: list[_Job], collection: _Collection, client: httpx.AsyncClient) -> None:
    # Waves prevent an outage from starting a new chunk/fallback before current attempts finish.
    for start in range(0, len(jobs), MAX_PROVIDER_CONCURRENCY):
        wave = jobs[start : start + MAX_PROVIDER_CONCURRENCY]
        tasks = [asyncio.create_task(_run_async(job, collection.budget, client)) for job in wave]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for job, result in zip(wave, results, strict=True):
            collection.accept(job, result)
        if collection.budget.reason() or _PROVIDER_CIRCUIT.blocked():
            for job in jobs[start + len(wave) :]:
                collection.accept(job, _stopped(collection.budget))
            break


def _stage_sync(jobs: list[_Job], collection: _Collection) -> None:
    for job in jobs:
        collection.accept(job, _run_sync(job, collection.budget))


def collect_identity_provider_results(
    inputs: list[dict[str, Any]],
    *,
    live_vpic: bool = True,
    live_wmi: bool = True,
    use_vpic_batch: bool = True,
    budget: IdentityBudget | None = None,
) -> dict[str, Any]:
    collection = _Collection(
        inputs,
        budget
        or IdentityBudget(
            max_attempts=SINGLE_HTTP_ATTEMPT_CAP if len(inputs) == 1 and not use_vpic_batch else BATCH_HTTP_ATTEMPT_CAP
        ),
    )
    collection.prepare(use_vpic_batch=use_vpic_batch)
    if live_vpic:
        _stage_sync(collection.primary_jobs, collection)
        _stage_sync(collection.fallback_jobs(), collection)
    if live_wmi:
        _stage_sync(collection.wmi_jobs(), collection)
    return collection.result(live_vpic=live_vpic, live_wmi=live_wmi)


async def collect_identity_provider_results_async(
    inputs: list[dict[str, Any]],
    *,
    live_vpic: bool = True,
    live_wmi: bool = True,
    use_vpic_batch: bool = True,
    budget: IdentityBudget | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    collection = _Collection(
        inputs,
        budget
        or IdentityBudget(
            max_attempts=SINGLE_HTTP_ATTEMPT_CAP if len(inputs) == 1 and not use_vpic_batch else BATCH_HTTP_ATTEMPT_CAP
        ),
    )
    collection.prepare(use_vpic_batch=use_vpic_batch)
    if not live_vpic and not live_wmi:
        return collection.result(live_vpic=False, live_wmi=False)
    if client is None:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as owned_client:
            return await _collect_async(collection, live_vpic=live_vpic, live_wmi=live_wmi, client=owned_client)
    return await _collect_async(collection, live_vpic=live_vpic, live_wmi=live_wmi, client=client)


async def _collect_async(
    collection: _Collection, *, live_vpic: bool, live_wmi: bool, client: httpx.AsyncClient
) -> dict[str, Any]:
    if live_vpic:
        await _stage_async(collection.primary_jobs, collection, client)
        await _stage_async(collection.fallback_jobs(), collection, client)
    if live_wmi:
        await _stage_async(collection.wmi_jobs(), collection, client)
    return collection.result(live_vpic=live_vpic, live_wmi=live_wmi)
