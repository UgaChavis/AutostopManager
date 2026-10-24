from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from autostop_manager import automation_control as control

UID = 20001
REQUEST_ID = "00000000-0000-4000-8000-000000000012"


class NoState:
    def __init__(self):
        self.accesses = []

    def __getattr__(self, name):
        self.accesses.append(name)
        raise AssertionError("unexpected state access")


class Service(control.AutomationControlService):
    def __init__(self, result=None):
        # Exercise actual handle validation/templates without opening state.
        self.store = NoState()
        self.requests = []
        self.result = result

    def handle(self, request):
        self.requests.append(deepcopy(request))
        actual = super().handle(request)
        return actual if self.result is None else self.result


class Writer:
    def __init__(self, uid):
        self.uid = uid
        self.output = b""
        self.close_calls = 0
        self.wait_closed_calls = 0

    def get_extra_info(self, name):
        assert name == "socket"
        return SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 123, self.uid, 20002))

    def write(self, data):
        self.output += data

    async def drain(self):
        pass

    def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_closed_calls += 1


@pytest.fixture(autouse=True)
def immediate_awaits(monkeypatch):
    async def wait(awaitable, timeout):
        assert timeout == 5
        return await awaitable

    async def dispatch(function, *args):
        return function(*args)

    monkeypatch.setattr(control.asyncio, "wait_for", wait)
    monkeypatch.setattr(control.asyncio, "to_thread", dispatch)


def request(request_id=REQUEST_ID, **values):
    return {
        "protocol": control.AUTOMATION_CONTROL_PROTOCOL,
        "request_id": request_id,
        "operation": "templates",
        "actor": {"kind": "codex", "id": "demo-echo"},
        "payload": {},
        **values,
    }


def serve(payload, *, uid=UID, service=None):
    original = deepcopy(payload)
    frame = json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"

    class Reader:
        async def readline(self):
            return frame

    service = service if service is not None else Service()
    writer = Writer(uid)
    server = control.AutomationControlServer(
        service=service,
        socket_path=Path("/synthetic-unused-echo.sock"),
        peer_roles={UID: frozenset({"codex"})},
        socket_gid=20002,
    )
    coroutine = server.serve_connection(Reader(), writer)
    try:
        with pytest.raises(StopIteration):
            coroutine.send(None)
    finally:
        coroutine.close()
        assert writer.close_calls == writer.wait_closed_calls == 1
        assert service.store.accesses == []
        assert payload == original
        assert service.requests in ([], [original])
    return json.loads(writer.output.decode("utf-8")), writer, service


def assert_error(actual, code, echo):
    assert actual == {
        "protocol": control.AUTOMATION_CONTROL_PROTOCOL,
        "request_id": echo,
        "ok": False,
        "error": {"code": code, "message": code},
    }


@pytest.mark.parametrize(
    "request_id", ["\ud800", "\udfff", "demo-\ud800-end", "\ud800" * 64], ids=["high", "low", "embedded", "length64"]
)
def test_invalid_unencodable_id_still_returns_actual_uuid_error(request_id):
    payload = request(request_id)
    actual, writer, service = serve(payload)
    assert_error(actual, "automation_request_id_invalid", "")
    assert service.requests == [payload]
    assert writer.output.endswith(b"\n")


@pytest.mark.parametrize("request_id", ["not-a-uuid", "запрос-α", "🚗", "  не UUID  "])
def test_encodable_invalid_id_keeps_original_echo_and_utf8_wire(request_id):
    actual, writer, service = serve(request(request_id))
    assert_error(actual, "automation_request_id_invalid", request_id)
    assert request_id.encode("utf-8") in writer.output
    assert len(service.requests) == 1


@pytest.mark.parametrize("request_id", [REQUEST_ID, "0" * 32, "{" + REQUEST_ID + "}"])
def test_valid_uuid_success_keeps_exact_echo_without_state(request_id):
    actual, _, service = serve(request(request_id))
    assert actual["ok"] is True
    assert actual["request_id"] == request_id
    assert isinstance(actual["data"]["templates"], list)
    assert service.requests == [request(request_id)]


@pytest.mark.parametrize("request_id", [None, 17, {"demo": 1}, "x" * 65, "\ud800" * 65])
def test_existing_nonstring_and_overlength_id_empty_echo_is_preserved(request_id):
    actual, _, service = serve(request(request_id))
    assert_error(actual, "automation_request_id_invalid", "")
    assert service.requests == [request(request_id)]


def test_missing_id_still_uses_empty_echo():
    payload = request()
    del payload["request_id"]
    actual, _, service = serve(payload)
    assert_error(actual, "automation_request_id_invalid", "")
    assert service.requests == [payload]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"unexpected": True}, "automation_request_fields_invalid"),
        ({"protocol": "demo"}, "automation_protocol_invalid"),
    ],
)
def test_nonutf8_echo_does_not_change_actual_service_validation_priority(changes, code):
    payload = request("\ud800", **changes)
    actual, _, service = serve(payload)
    assert_error(actual, code, "")
    assert service.requests == [payload]


@pytest.mark.parametrize("gate", ["uid", "role"])
def test_nonutf8_echo_preserves_peer_gate_priority_without_dispatch(gate):
    payload = request("\ud800")
    if gate == "role":
        payload["actor"]["kind"] = "crm_operator"
    actual, _, service = serve(payload, uid=UID + 1 if gate == "uid" else UID)
    assert_error(actual, "automation_peer_not_allowed" if gate == "uid" else "automation_peer_role_not_allowed", "")
    assert service.requests == []


def test_service_output_unicode_failure_is_not_sanitized():
    with pytest.raises(UnicodeEncodeError):
        serve(request(), service=Service({"demo": "\ud800"}))


def test_response_byte_limit_still_returns_its_existing_error(monkeypatch):
    monkeypatch.setattr(control, "MAX_RESPONSE_BYTES", 300)
    actual, _, service = serve(request(), service=Service({"demo": "x" * 1000}))
    assert_error(actual, "automation_response_too_large", REQUEST_ID)
    assert len(service.requests) == 1
