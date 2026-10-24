from __future__ import annotations

import threading
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request

import pytest

from autostop_manager import bounded_http_read as bounded


@pytest.mark.parametrize(
    ("stage", "failure_type", "unavailable"),
    [
        ("event", RuntimeError, False),
        ("event", KeyboardInterrupt, False),
        ("lock", MemoryError, False),
        ("constructor", MemoryError, False),
        ("constructor", RuntimeError, True),
        ("start", RuntimeError, True),
    ],
)
def test_failure_before_worker_dispatch_preserves_transport_capacity(monkeypatch, stage, failure_type, unavailable):
    admission = bounded.TrackedTransportAdmission(1)
    calls = {"event": 0, "lock": 0, "constructor": 0, "start": 0, "fetch": 0}

    def enter(name):
        calls[name] += 1
        if stage == name:
            raise failure_type("synthetic HTTP worker setup failure")

    def unexpected_wait(*_args):
        pytest.fail("a rejected worker must not reach the response wait")

    def event():
        enter("event")
        return SimpleNamespace(wait=unexpected_wait, set=lambda: None)

    def lock():
        enter("lock")
        return threading.Lock()

    def thread(*, target, name, daemon):
        assert callable(target)
        assert name == "autostop-bounded-http" and daemon is True
        enter("constructor")

        def start():
            enter("start")
            pytest.fail("the synthetic worker must be rejected before execution")

        return SimpleNamespace(start=start)

    async def fetch(*_args, **_kwargs):
        calls["fetch"] += 1
        pytest.fail("setup failures must not dispatch an HTTP request")

    # Keep the real threading module intact for pytest and other components.
    monkeypatch.setattr(bounded, "threading", SimpleNamespace(Event=event, Lock=lock, Thread=thread))
    monkeypatch.setattr(bounded, "_fetch", fetch)

    with pytest.raises(URLError if unavailable else failure_type) as failure:
        bounded.urlopen(Request("https://synthetic.invalid/"), timeout=1, maximum=20, admission=admission)

    if unavailable:
        assert failure.value.reason == "provider_transport_unavailable"
    else:
        assert str(failure.value) == "synthetic HTTP worker setup failure"
    assert calls["fetch"] == 0
    assert calls["start"] == (stage == "start")
    assert admission.pending_count == 0
    assert admission.acquire(blocking=False)
    admission.release()
    assert admission.pending_count == 0


@pytest.mark.parametrize("mode", ["occupied", "draining"])
def test_rejected_admission_does_not_start_preparing_another_worker(monkeypatch, mode):
    admission = bounded.TrackedTransportAdmission(1)
    if mode == "occupied":
        assert admission.acquire(blocking=False)
    else:
        admission.begin_drain()

    def unexpected_setup(*_args, **_kwargs):
        pytest.fail("rejected admission must retain priority over setup failures")

    monkeypatch.setattr(
        bounded,
        "threading",
        SimpleNamespace(Event=unexpected_setup, Lock=unexpected_setup, Thread=unexpected_setup),
    )
    try:
        with pytest.raises(URLError) as failure:
            bounded.urlopen(Request("https://synthetic.invalid/"), timeout=1, maximum=20, admission=admission)
        assert failure.value.reason == "provider_transport_busy"
        assert admission.pending_count == (mode == "occupied")
    finally:
        if mode == "occupied":
            admission.release()
