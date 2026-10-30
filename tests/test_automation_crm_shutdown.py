from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

import anyio
import httpx
import pytest

from autostop_manager import automation_daemon as daemon_module
from autostop_manager import automation_jobs as jobs
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from tests.test_automation_crm_http_deadline import (
    claimed_store as claimed_store,
    endpoint as endpoint,
    source,
    wait_idle,
)
from tests.test_automation_crm_response_bounds import READINESS
from tests.test_automation_jobs import FakeNotifier, FakeSource


def daemon_case(monkeypatch, admission, *, close_error=None, readiness_admission=None):
    events = []
    stopping = asyncio.Event()

    class Store:
        def initialize(self):
            pass

        def seed_defaults(self):
            pass

        def controller_heartbeat(self, **values):
            events.append(values.get("state", "active"))

    class Server:
        closed = False
        closes = 0

        async def start(self):
            pass

        async def close(self):
            assert admission.pending_count == 0
            assert readiness_admission is None or readiness_admission.pending_count == 0
            self.closes += 1
            events.append("control_close")
            if close_error is not None:
                raise close_error
            self.closed = True

    def notify(message):
        if message == "STOPPING=1":
            stopping.set()

    monkeypatch.setattr(daemon_module, "reject_obsolete_runtime_config", lambda: None)
    monkeypatch.setattr(daemon_module, "notify_systemd", notify)
    # Patch this test's signal registration, never asyncio's global loop getter:
    # the owned HTTP worker has its own real event loop and DNS executor.
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    server = Server()
    daemon = daemon_module.AutomationDaemon(
        store=Store(),
        timer_controller=SimpleNamespace(adopt_all_current=lambda: None),
        control_server=server,
        crm_http_admission=admission,
        crm_readiness_admission=readiness_admission,
    )
    return SimpleNamespace(daemon=daemon, events=events, stopping=stopping, server=server)


@pytest.mark.parametrize("failure", [None, "scheduler", "parent_cancel", "close"])
def test_shutdown_waits_for_timed_out_http_cleanup_and_preserves_failures(monkeypatch, failure):
    async def scenario():
        admission = TrackedTransportAdmission(1)
        release, closing = threading.Event(), threading.Event()
        error = RuntimeError("synthetic cleanup failure")
        case = daemon_case(monkeypatch, admission, close_error=error if failure == "close" else None)
        attempts = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"{"
                raise httpx.ReadError("synthetic incomplete response")

            async def aclose(self):
                closing.set()
                with anyio.CancelScope(shield=True):
                    await asyncio.to_thread(release.wait)

        def respond(request):
            attempts.append(request.url.host)
            if request.url.host == "owned.invalid":
                return httpx.Response(200, stream=Body())
            return httpx.Response(200, json={"ok": True, "data": READINESS})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(respond)
        monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
        client = source("https://owned.invalid", admission=admission)

        async def scheduler():
            try:
                await asyncio.to_thread(client.register)
            except jobs.AutomationJobError as exc:
                assert exc.outcome_uncertain is True
                case.events.append("caller_timeout")
            finally:
                case.daemon.stopping.set()
            if failure == "scheduler":
                raise error

        monkeypatch.setattr(case.daemon, "scheduler_loop", scheduler)
        running = asyncio.create_task(case.daemon.run())
        try:
            await asyncio.wait_for(case.stopping.wait(), 3)
            assert closing.is_set() and admission.pending_count == 1
            assert not running.done() and not case.server.closed
            assert "stopped" not in case.events and case.server.closes == 0
            with pytest.raises(jobs.AutomationJobError) as rejected:
                client.register()
            assert rejected.value.outcome_uncertain is False
            assert attempts == ["owned.invalid"]
            # A daemon owns only its supplied pool. Direct callers and another
            # daemon's pool remain usable while this one is draining.
            assert source("https://direct.invalid").readiness() == READINESS
            other_pool = TrackedTransportAdmission(1)
            other = daemon_case(monkeypatch, other_pool)
            assert other.daemon._crm_http_admission is not case.daemon._crm_http_admission
            assert source("https://other.invalid", admission=other_pool).readiness() == READINESS
            if failure == "parent_cancel":
                running.cancel("during CRM drain")
                await asyncio.sleep(0)
                running.cancel("again")
                await asyncio.sleep(0)
                assert not running.done() and admission.pending_count == 1
            release.set()
            if failure in {"scheduler", "close"}:
                with pytest.raises(RuntimeError) as caught:
                    await asyncio.wait_for(asyncio.shield(running), 3)
                assert caught.value is error
            elif failure == "parent_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(running), 3)
            else:
                await asyncio.wait_for(asyncio.shield(running), 3)
            assert admission.pending_count == 0 and case.server.closes == 1
            assert case.server.closed is (failure != "close")
            if failure == "scheduler":
                assert "stopped" not in case.events
            else:
                assert case.events.index("stopped") < case.events.index("control_close")
            assert attempts.count("owned.invalid") == 1
        finally:
            release.set()
            case.daemon.stopping.set()
            await asyncio.gather(running, return_exceptions=True)
            await asyncio.to_thread(wait_idle, admission)

    asyncio.run(scenario())


def test_shutdown_stays_stopping_until_dns_worker_finishes(endpoint, monkeypatch):
    async def scenario():
        admission = TrackedTransportAdmission(1)
        release, resolving = threading.Event(), threading.Event()
        original = socket.getaddrinfo
        case = daemon_case(monkeypatch, admission)

        def resolve(host, *args, **kwargs):
            if host in {"crm.synthetic", b"crm.synthetic"}:
                resolving.set()
                release.wait()
                host = "127.0.0.1"
            return original(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        client = source(endpoint.url.replace("127.0.0.1", "crm.synthetic"), admission=admission)

        async def scheduler():
            try:
                await asyncio.to_thread(client.register)
            except jobs.AutomationJobError as exc:
                assert exc.outcome_uncertain is True
            finally:
                case.daemon.stopping.set()

        monkeypatch.setattr(case.daemon, "scheduler_loop", scheduler)
        running = asyncio.create_task(case.daemon.run())
        try:
            await asyncio.wait_for(case.stopping.wait(), 3)
            assert resolving.is_set() and admission.pending_count == 1
            running.cancel("during DNS drain")
            await asyncio.sleep(0.05)
            assert not running.done() and not case.server.closed
            assert "stopped" not in case.events and endpoint.calls == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 3)
            assert admission.pending_count == 0 and case.server.closed
            assert endpoint.calls == []
        finally:
            release.set()
            case.daemon.stopping.set()
            await asyncio.gather(running, return_exceptions=True)
            await asyncio.to_thread(wait_idle, admission)

    asyncio.run(scenario())


@pytest.mark.parametrize("readiness_inflight", [False, True])
def test_shutdown_finishes_current_multistage_digest_through_exact_ack(
    endpoint, claimed_store, monkeypatch, readiness_inflight
):
    async def scenario():
        pool = TrackedTransportAdmission(1)
        readiness_pool = TrackedTransportAdmission(1) if readiness_inflight else None
        case = daemon_case(monkeypatch, pool, readiness_admission=readiness_pool)
        store, _service, _job, claim = claimed_store
        client = source(endpoint.url, timeout=3, admission=pool)
        fake = FakeSource()
        fake.register, fake.acknowledge = client.register, client.acknowledge
        notifier = FakeNotifier()
        execute = jobs.CrmDigestExecutor(store=store, source=fake, notifier=notifier)
        results = []
        digest_done = asyncio.Event()
        readiness_task = None
        endpoint.mode = "fast"
        endpoint.release_request.clear()

        async def scheduler():
            results.append(await asyncio.to_thread(execute, claim))
            digest_done.set()

        monkeypatch.setattr(case.daemon, "scheduler_loop", scheduler)
        running = asyncio.create_task(case.daemon.run())
        try:
            assert await asyncio.to_thread(endpoint.received.wait, 2)
            if readiness_inflight:
                endpoint.release_readiness.clear()
                readiness_client = source(endpoint.url, timeout=4, admission=readiness_pool)
                readiness_task = asyncio.create_task(asyncio.to_thread(readiness_client.readiness))
                assert await asyncio.to_thread(endpoint.readiness_received.wait, 2)
            case.daemon.stopping.set()
            await asyncio.wait_for(case.stopping.wait(), 2)
            assert not running.done() and not case.server.closed
            if readiness_inflight:
                with pytest.raises(jobs.AutomationJobError) as rejected:
                    readiness_client.readiness()
                assert rejected.value.outcome_uncertain is False
            endpoint.release_request.set()
            await asyncio.wait_for(digest_done.wait(), 4)
            assert results == [{"ok": True, "result_code": "ok", "pages": 1, "events": 3}], {
                "results": results,
                "notifications": len(notifier.applied),
                "http_operations": [operation for operation, _payload in endpoint.calls],
            }
            assert [call for call in endpoint.calls if call[0] != "readiness"] == [
                ("register", {"consumer_id": jobs.CRM_DIGEST_CONSUMER_ID, "start_at": "latest"}),
                ("ack", {"consumer_id": jobs.CRM_DIGEST_CONSUMER_ID, "ack": "full-window-ack"}),
            ]
            assert len(notifier.applied) == 1
            assert all(item["value"] is None for item in store.get_cursors(job_id=claim["job_id"]).values())
            if readiness_inflight:
                assert readiness_pool.pending_count == 1
                assert not running.done() and not case.server.closed
                assert "stopped" not in case.events
                endpoint.release_readiness.set()
                assert await readiness_task == READINESS
                assert [operation for operation, _payload in endpoint.calls].count("readiness") == 1
            await asyncio.wait_for(asyncio.shield(running), 5)
            assert pool.pending_count == 0 and case.server.closed
        finally:
            endpoint.release_request.set()
            endpoint.release_readiness.set()
            case.daemon.stopping.set()
            if readiness_task is not None:
                await asyncio.gather(readiness_task, return_exceptions=True)
            await asyncio.gather(running, return_exceptions=True)
            await asyncio.to_thread(wait_idle, pool)

    asyncio.run(scenario())


def test_scheduler_and_readiness_cannot_share_shutdown_admission():
    pool = TrackedTransportAdmission(1)
    with pytest.raises(ValueError, match="automation_crm_admissions_must_be_separate"):
        daemon_module.AutomationDaemon(crm_http_admission=pool, crm_readiness_admission=pool)
