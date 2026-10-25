from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from autostop_manager import automation_control


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class TimedSocket:
    def __init__(self, clock, *, connect=0.0, send=0.0, read=0.0, parts=3):
        self.clock = clock
        self.durations = {"connect": connect, "send": send, "read": read}
        self.parts = parts
        self.timeout = None
        self.timeouts = []
        self.sent = []
        self.chunks = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def _wait(self, operation):
        duration = self.durations[operation]
        if self.timeout == 0 and duration:
            raise BlockingIOError("synthetic nonblocking socket")
        if self.timeout is not None and duration > self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError("synthetic socket wait expired")
        self.clock.now += duration

    def connect(self, _path):
        self._wait("connect")

    def sendall(self, body):
        request = json.loads(body)
        self.sent.append(request)
        self._wait("send")
        response = (
            json.dumps(
                {"protocol": request["protocol"], "request_id": request["request_id"], "ok": True, "data": {}}
            ).encode()
            + b"\n"
        )
        width = (len(response) + self.parts - 1) // self.parts
        self.chunks = [response[index : index + width] for index in range(0, len(response), width)]

    def recv(self, _size):
        self._wait("read")
        return self.chunks.pop(0) if self.chunks else b""


def make_client(monkeypatch, tmp_path, *, timeout=10, **durations):
    clock = Clock()
    peer = TimedSocket(clock, **durations)
    monkeypatch.setattr(automation_control, "time", SimpleNamespace(monotonic=clock.monotonic), raising=False)
    monkeypatch.setattr(automation_control.socket, "socket", lambda *_args: peer)
    client = automation_control.AutomationControlClient(socket_path=tmp_path / "unused.sock", timeout=timeout)
    return client, peer, clock


def test_fragmented_response_within_total_budget_keeps_the_normal_result(monkeypatch, tmp_path):
    client, peer, clock = make_client(monkeypatch, tmp_path, timeout=2, read=0.5, parts=3)

    result = client.request("status")

    assert result["ok"] is True
    assert result["data"] == {}
    assert clock.now == pytest.approx(1.5)
    assert len(peer.sent) == 1
    assert peer.closed


def test_progressing_response_cannot_restart_the_total_budget(monkeypatch, tmp_path):
    client, peer, clock = make_client(monkeypatch, tmp_path, timeout=1, read=0.9, parts=6)

    with pytest.raises(TimeoutError):
        client.request("status")

    assert clock.now == pytest.approx(1)
    assert peer.closed
    assert len(peer.sent) == 1


def test_connect_send_and_response_share_one_budget_without_retrying_a_write(monkeypatch, tmp_path):
    client, peer, clock = make_client(monkeypatch, tmp_path, connect=6, send=3, read=3, parts=1)

    with pytest.raises(TimeoutError):
        client.request(
            "set_enabled",
            {"job_id": "synthetic-job", "enabled": False},
            idempotency_key="synthetic-key",
            expected_revision=7,
        )

    assert clock.now == pytest.approx(10)
    assert peer.closed
    assert len(peer.sent) == 1
    request = peer.sent[0]
    assert request["operation"] == "set_enabled"
    assert request["payload"] == {"job_id": "synthetic-job", "enabled": False}
    assert request["idempotency_key"] == "synthetic-key"
    assert request["expected_revision"] == 7


def test_complete_frame_cannot_bypass_an_expired_budget(monkeypatch, tmp_path):
    client, peer, clock = make_client(monkeypatch, tmp_path, timeout=1, read=1, parts=1)

    with pytest.raises(TimeoutError):
        client.request("status")

    assert clock.now == 1
    assert peer.closed
    assert len(peer.sent) == 1


@pytest.mark.parametrize("timeout", [None, 0])
def test_explicit_blocking_and_nonblocking_modes_keep_immediate_responses(monkeypatch, tmp_path, timeout):
    client, peer, clock = make_client(monkeypatch, tmp_path, timeout=timeout, parts=1)

    assert client.request("status")["ok"] is True
    assert peer.closed
    assert peer.timeouts == [timeout]
    assert clock.now == 0
