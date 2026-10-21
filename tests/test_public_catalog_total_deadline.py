"""Total HTTP budgets, cancellation and admission with no external network.

Both client protocols use the same synthetic transport so the regressions also
exercise the synchronous baseline. A continuous body always makes progress;
event gates hold DNS, headers and cleanup without timing those operations.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from autostop_manager import public_catalog_http as catalog


URL = "https://www.elcats.ru/vw/?model=synthetic"
REQUEST_SECONDS = 1.0
WATCHDOG_SECONDS = 5.0


class Gate:
    def __init__(self):
        self.opened = threading.Event()
        self.loop = None
        self.async_event = None

    def wait(self):
        # The test's finally block releases this gate. A watchdog here would
        # incorrectly make the old worker stop even without cancellation.
        self.opened.wait()

    async def await_open(self):
        self.loop = asyncio.get_running_loop()
        self.async_event = asyncio.Event()
        if not self.opened.is_set():
            await self.async_event.wait()

    def open(self):
        self.opened.set()
        if self.loop is not None and self.async_event is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.async_event.set)
            except RuntimeError:
                if not self.loop.is_closed():
                    raise


class Admission:
    def __init__(self):
        self.semaphore = threading.BoundedSemaphore(1)
        self.released = threading.Event()

    def acquire(self, *, blocking):
        return self.semaphore.acquire(blocking=blocking)

    def release(self):
        self.semaphore.release()
        self.released.set()


class ProgressBody(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, cleanup, *, continuous=True):
        self.cleanup = cleanup
        self.continuous = continuous
        self.started = threading.Event()
        self.stop = threading.Event()
        self.close_started = threading.Event()
        self.close_cancelled = threading.Event()
        self.after_budget = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.started.set()
        while not self.stop.is_set():
            yield b"x"
            if not self.continuous:
                return

    async def __aiter__(self):
        self.started.set()
        while not self.stop.is_set():
            yield b"x"
            if not self.continuous:
                return
            # Yield execution while keeping the response continuously readable.
            await asyncio.sleep(0)

    def close(self):
        self.close_started.set()
        self.cleanup.wait()
        self.closed.set()

    async def aclose(self):
        self.close_started.set()
        if not self.continuous:
            # Keep EOF cleanup pending beyond the request budget. The signal
            # runs outside the cancelled request task and releases no gate.
            asyncio.get_running_loop().call_later(2 * REQUEST_SECONDS, self.after_budget.set)
        try:
            await self.cleanup.await_open()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            raise
        self.closed.set()


class Scenario:
    def __init__(self, monkeypatch, *, phase=None, failure=None):
        self.phase = phase
        self.failure = failure
        self.headers_started = threading.Event()
        self.headers_finished = threading.Event()
        self.headers_gate = Gate()
        self.setup_started = threading.Event()
        self.setup_gate = Gate()
        self.cleanup = Gate()
        self.body = ProgressBody(self.cleanup, continuous=phase != "eof_cleanup")
        self.client_close_started = threading.Event()
        self.client_closed = threading.Event()
        self.async_client_used = threading.Event()
        self.requests = []
        self.client_options = []
        self.admission = Admission()
        monkeypatch.setattr(catalog, "_ADMISSIONS", self.admission)
        monkeypatch.setattr(catalog, "_public_address", lambda *_args: "8.8.8.8")
        real_client = httpx.Client
        real_async_client = httpx.AsyncClient
        scenario = self

        class MockTransport(httpx.MockTransport):
            def close(self):
                scenario.client_close_started.set()
                scenario.cleanup.wait()
                scenario.client_closed.set()

            async def aclose(self):
                scenario.client_close_started.set()
                await scenario.cleanup.await_open()
                scenario.client_closed.set()

        def client(**kwargs):
            self.client_options.append(kwargs)
            self.setup_started.set()
            self.setup_gate.wait()
            return real_client(transport=MockTransport(self.handle), **kwargs)

        def async_client(**kwargs):
            self.async_client_used.set()
            self.client_options.append(kwargs)
            self.setup_started.set()
            self.setup_gate.wait()
            return real_async_client(transport=MockTransport(self.async_handle), **kwargs)

        monkeypatch.setattr(catalog.httpx, "Client", client)
        monkeypatch.setattr(catalog.httpx, "AsyncClient", async_client)
        if phase not in {"headers", "body", "eof_cleanup"}:
            self.cleanup.open()
        if phase != "client_setup":
            self.setup_gate.open()

    def response(self, request):
        if self.failure is not None:
            raise self.failure
        if len(self.requests) == 1 and self.phase in {"body", "eof_cleanup"}:
            return httpx.Response(200, stream=self.body)
        return httpx.Response(200, content=b"ok")

    def handle(self, request):
        self.requests.append(request)
        if len(self.requests) == 1 and self.phase == "headers":
            self.headers_started.set()
            try:
                self.headers_gate.wait()
            finally:
                self.headers_finished.set()
        return self.response(request)

    async def async_handle(self, request):
        self.requests.append(request)
        if len(self.requests) == 1 and self.phase == "headers":
            self.headers_started.set()
            try:
                await self.headers_gate.await_open()
            finally:
                self.headers_finished.set()
        return self.response(request)

    def release(self):
        self.body.stop.set()
        self.headers_gate.open()
        self.setup_gate.open()
        self.cleanup.open()


def start_request(*, maximum=1024 * 1024 * 1024):
    done = threading.Event()
    outcomes = []

    def request():
        try:
            outcomes.append(catalog._bounded_get(URL, REQUEST_SECONDS, maximum))
        except BaseException as exc:  # noqa: BLE001 - inspect the caller's complete outcome.
            outcomes.append(exc)
        finally:
            done.set()

    caller = threading.Thread(target=request, name="synthetic-catalog-caller", daemon=True)
    caller.start()
    return caller, done, outcomes


def assert_deadline(done, outcomes):
    assert done.wait(WATCHDOG_SECONDS), "catalog caller did not finish"
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], catalog.CatalogReadError)
    assert outcomes[0].code == "catalog_deadline_exceeded"
    assert outcomes[0].retryable is True


@pytest.mark.parametrize("phase", ["headers", "body", "eof_cleanup"])
def test_total_deadline_stops_transfer_and_retains_admission_until_cleanup(monkeypatch, phase):
    scenario = Scenario(monkeypatch, phase=phase)
    caller, done, outcomes = start_request()
    try:
        started = scenario.headers_started if phase == "headers" else scenario.body.started
        assert started.wait(WATCHDOG_SECONDS), "request did not reach its synthetic transfer"
        assert_deadline(done, outcomes)
        closing = scenario.client_close_started if phase == "headers" else scenario.body.close_started
        assert closing.wait(WATCHDOG_SECONDS), "deadline returned while the HTTP worker kept transferring"
        if phase == "headers":
            assert scenario.headers_finished.is_set()
        elif phase == "eof_cleanup" and scenario.async_client_used.is_set():
            assert scenario.body.after_budget.wait(WATCHDOG_SECONDS)
            assert not scenario.body.close_cancelled.is_set(), "deadline interrupted EOF stream cleanup"
        assert not scenario.admission.released.is_set()
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_transport_busy$"):
            catalog._bounded_get(URL, REQUEST_SECONDS, 16)
        assert len(scenario.requests) == 1

        scenario.cleanup.open()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS), "cleanup did not return admission"
        assert scenario.client_closed.is_set()
        if phase != "headers":
            assert scenario.body.closed.is_set()
        assert catalog._bounded_get(URL, REQUEST_SECONDS, 16)[:2] == (200, b"ok")
    finally:
        scenario.release()
        caller.join(WATCHDOG_SECONDS)
        assert not caller.is_alive()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS), "fixture left an HTTP worker running"


def test_dns_finishing_after_caller_deadline_never_starts_http(monkeypatch):
    scenario = Scenario(monkeypatch)
    dns_started = threading.Event()
    dns_gate = Gate()

    def resolve(_host, _port):
        dns_started.set()
        dns_gate.wait()
        return "8.8.8.8"

    monkeypatch.setattr(catalog, "_public_address", resolve)
    caller, done, outcomes = start_request()
    try:
        assert dns_started.wait(WATCHDOG_SECONDS)
        assert_deadline(done, outcomes)
        assert not scenario.admission.released.is_set()
        assert not scenario.client_options
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_transport_busy$"):
            catalog._bounded_get(URL, REQUEST_SECONDS, 16)

        dns_gate.open()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS)
        assert not scenario.client_options, "expired DNS result started a new HTTP client"
        assert not scenario.requests
        assert catalog._bounded_get(URL, REQUEST_SECONDS, 16)[:2] == (200, b"ok")
    finally:
        dns_gate.open()
        scenario.release()
        caller.join(WATCHDOG_SECONDS)
        assert not caller.is_alive()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS)


@pytest.mark.parametrize("phase", ["worker_startup", "client_setup"])
def test_expired_startup_never_dispatches_late_http(monkeypatch, phase):
    scenario = Scenario(monkeypatch, phase=phase)
    dns_calls = []

    def resolve(host, port):
        dns_calls.append((host, port))
        return "8.8.8.8"

    monkeypatch.setattr(catalog, "_public_address", resolve)
    worker_started = threading.Event()
    startup_gate = Gate()
    workers = []
    if phase == "worker_startup":

        def delayed_thread(*, target, **kwargs):
            def delayed_target():
                worker_started.set()
                startup_gate.wait()
                target()

            worker = threading.Thread(target=delayed_target, **kwargs)
            workers.append(worker)
            return worker

        # Replace only this module's threading reference. The caller and
        # pytest retain real Thread.start, which returns before the gate opens.
        monkeypatch.setattr(catalog, "threading", SimpleNamespace(Thread=delayed_thread, Event=threading.Event))
    caller, done, outcomes = start_request()
    gate = startup_gate if phase == "worker_startup" else scenario.setup_gate
    started = worker_started if phase == "worker_startup" else scenario.setup_started
    try:
        assert started.wait(WATCHDOG_SECONDS)
        assert_deadline(done, outcomes)
        assert not scenario.admission.released.is_set()
        assert not scenario.requests
        with pytest.raises(catalog.CatalogReadError, match=r"^catalog_transport_busy$"):
            catalog._bounded_get(URL, REQUEST_SECONDS, 16)

        gate.open()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS)
        assert not scenario.requests, "expired startup dispatched a new HTTP request"
        if phase == "worker_startup":
            assert not dns_calls, "expired worker began DNS resolution"
            assert not scenario.client_options
        else:
            assert dns_calls == [("www.elcats.ru", 443)]
            assert scenario.client_closed.is_set()
        assert catalog._bounded_get(URL, REQUEST_SECONDS, 16)[:2] == (200, b"ok")
    finally:
        startup_gate.open()
        scenario.release()
        caller.join(WATCHDOG_SECONDS)
        assert not caller.is_alive()
        assert scenario.admission.released.wait(WATCHDOG_SECONDS)
        for worker in workers:
            worker.join(WATCHDOG_SECONDS)
            assert not worker.is_alive()


def test_transport_keeps_pinned_address_host_sni_and_disables_environment(monkeypatch):
    scenario = Scenario(monkeypatch)
    assert catalog._bounded_get(URL, REQUEST_SECONDS, 16)[:2] == (200, b"ok")
    assert len(scenario.requests) == len(scenario.client_options) == 1
    request = scenario.requests[0]
    assert str(request.url) == "https://8.8.8.8/vw/?model=synthetic"
    assert request.headers["Host"] == "www.elcats.ru"
    assert request.headers["User-Agent"] == catalog.CATALOG_USER_AGENT
    assert request.headers["Accept-Encoding"] == "identity"
    assert request.extensions["sni_hostname"] == "www.elcats.ru"
    assert scenario.client_options[0]["trust_env"] is False
    assert scenario.client_options[0]["follow_redirects"] is False
    assert scenario.client_closed.is_set()
    assert scenario.admission.released.is_set()


@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ConnectTimeout, TimeoutError])
def test_transport_timeout_keeps_its_existing_classification_and_releases_admission(monkeypatch, failure_type):
    scenario = Scenario(monkeypatch, failure=failure_type("synthetic timeout"))
    with pytest.raises(catalog.CatalogReadError, match=r"^catalog_timeout$") as raised:
        catalog._bounded_get(URL, REQUEST_SECONDS, 16)
    assert raised.value.retryable is True
    assert scenario.client_closed.is_set()
    assert scenario.admission.released.is_set()
