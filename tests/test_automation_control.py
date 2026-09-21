from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from autostop_manager import automation_control
from autostop_manager.automation_control import (
    AUTOMATION_CONTROL_PROTOCOL,
    AutomationControlClient,
    AutomationControlServer,
    AutomationControlService,
)
from autostop_manager.automation_registry import AutomationError, AutomationStore


def test_unix_protocol_round_trip_and_peer_credentials(tmp_path: Path):
    async def scenario():
        socket_path = tmp_path / "control.sock"
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)
        service = AutomationControlService(AutomationStore(state_dir / "manager.sqlite3"))
        server = AutomationControlServer(
            service=service,
            socket_path=socket_path,
            allowed_uids=frozenset({os.getuid()}),
        )
        await server.start()
        try:
            client = AutomationControlClient(socket_path=socket_path)
            response = await asyncio.to_thread(client.request, "status")
            socket_mode = stat.S_IMODE(socket_path.stat().st_mode)
            directory_mode = stat.S_IMODE(socket_path.parent.stat().st_mode)
        finally:
            await server.close()
        return response, socket_path, socket_mode, directory_mode

    response, socket_path, socket_mode, directory_mode = asyncio.run(scenario())

    assert response["protocol"] == AUTOMATION_CONTROL_PROTOCOL
    assert response["ok"] is True
    assert response["data"]["jobs"] == []
    assert response["data"]["templates"][0]["template_id"] == "crm_digest_v1"
    assert socket_mode == 0o660
    assert directory_mode == 0o750
    assert not socket_path.exists()


def test_unix_protocol_rejects_unlisted_peer_uid(tmp_path: Path):
    async def scenario():
        socket_path = tmp_path / "control.sock"
        server = AutomationControlServer(
            service=AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3")),
            socket_path=socket_path,
            allowed_uids=frozenset({os.getuid() + 10000}),
        )
        await server.start()
        try:
            client = AutomationControlClient(socket_path=socket_path)
            return await asyncio.to_thread(client.request, "status")
        finally:
            await server.close()

    response = asyncio.run(scenario())

    assert response["ok"] is False
    assert response["error"]["code"] == "automation_peer_not_allowed"


def test_socket_server_rejects_unsafe_preexisting_path(tmp_path: Path):
    async def scenario():
        socket_path = tmp_path / "control.sock"
        socket_path.write_text("not a socket", encoding="utf-8")
        server = AutomationControlServer(socket_path=socket_path, allowed_uids=frozenset({os.getuid()}))
        try:
            await server.start()
        except AutomationError as exc:
            return str(exc)
        raise AssertionError("unsafe path accepted")

    assert asyncio.run(scenario()) == "automation_socket_path_unsafe"


def test_peer_uid_cannot_claim_a_different_actor_role(tmp_path: Path):
    async def scenario():
        socket_path = tmp_path / "control.sock"
        server = AutomationControlServer(
            service=AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3")),
            socket_path=socket_path,
            peer_roles={os.getuid(): frozenset({"crm_operator"})},
        )
        await server.start()
        try:
            client = AutomationControlClient(
                socket_path=socket_path,
                actor={"kind": "codex", "id": "spoofed", "is_admin": True},
            )
            return await asyncio.to_thread(client.request, "status")
        finally:
            await server.close()

    response = asyncio.run(scenario())

    assert response["ok"] is False
    assert response["error"]["code"] == "automation_peer_role_not_allowed"


def test_readiness_returns_fresh_privacy_safe_execution_packet(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_REVISION", "b" * 40)
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CRM_VERSION", "crm-1.2.3")
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CRM_REVISION", "c" * 40)
    store = AutomationStore(tmp_path / "manager.sqlite3")
    seeded = store.seed_defaults()["job"]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO manager_automation_cursors(job_id, cursor_name, cursor_value, revision, updated_at)
            VALUES(?, 'pending_ack', ?, 7, '2026-09-21T12:00:00Z')
            """,
            (seeded["job_id"], "TOP-SECRET-CURSOR-VALUE"),
        )
    service = AutomationControlService(store)
    response = service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": "readiness",
            "actor": {"kind": "codex", "id": "readiness-test", "is_admin": True},
            "payload": {},
        }
    )

    packet = response["execution_packet"]
    assert packet["format"] == "manager_automation_execution_packet_v1"
    assert packet["manager_revision"] == "b" * 40
    assert packet["crm"] == {"version": "crm-1.2.3", "revision": "c" * 40}
    assert {item["path_label"] for item in packet["instruction_hashes"]} == {
        "AGENTS.md",
        ".agents/skills/manage-owner-telegram/SKILL.md",
        ".agents/skills/manage-autostop-store/SKILL.md",
        "docs/agent/deployment_runbook.md",
    }
    assert packet["jobs"][0]["revision"] == 1
    assert packet["jobs"][0]["applied_revision"] == 1
    assert packet["cursors"] == [
        {
            "job_id": seeded["job_id"],
            "name": "pending_ack",
            "revision": 7,
            "present": True,
        }
    ]
    assert packet["runs"] == {"by_status": {}, "total_count": 0}
    assert packet["outbox"]["total_count"] == 0
    serialized = json.dumps(response)
    assert "TOP-SECRET-CURSOR-VALUE" not in serialized
    assert "bearer" not in serialized.lower()


def _base_request(*, operation="status", payload=None, actor=None):
    return {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": actor or {"kind": "codex", "id": "coverage-test", "is_admin": True},
        "payload": {} if payload is None else payload,
    }


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda request: request | {"unexpected": True}, "automation_request_fields_invalid"),
        (lambda request: request | {"protocol": "wrong"}, "automation_protocol_invalid"),
        (lambda request: request | {"request_id": ""}, "automation_request_id_invalid"),
        (lambda request: request | {"request_id": "not-a-uuid"}, "automation_request_id_invalid"),
        (lambda request: request | {"operation": "bad\x01operation"}, "automation_operation_invalid"),
        (lambda request: request | {"operation": "unknown"}, "automation_operation_invalid"),
        (lambda request: request | {"actor": []}, "automation_actor_invalid"),
        (
            lambda request: request | {"actor": {"kind": "codex", "id": "test", "is_admin": True, "extra": True}},
            "automation_actor_invalid",
        ),
        (
            lambda request: request | {"actor": {"kind": "unknown", "id": "test", "is_admin": True}},
            "automation_actor_invalid",
        ),
        (lambda request: request | {"payload": []}, "automation_payload_invalid"),
        (lambda request: request | {"payload": {"value": "x" * (33 * 1024)}}, "automation_payload_too_large"),
        (lambda request: request | {"expected_revision": True}, "expected_revision_invalid"),
        (lambda request: request | {"expected_revision": -1}, "expected_revision_invalid"),
        (
            lambda request: request | {"payload": {"job_id": 7}},
            "automation_job_id_invalid",
        ),
        (
            lambda request: request | {"operation": "readiness", "payload": {"job_id": 7}},
            "automation_job_id_invalid",
        ),
        (
            lambda request: request | {"operation": "archive", "payload": {"job_id": "missing"}},
            "idempotency_key_required",
        ),
    ],
)
def test_control_request_validation_fails_closed(tmp_path, mutate, error_code):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))

    with pytest.raises(AutomationError, match=error_code):
        service.handle(mutate(_base_request()))


def test_templates_and_instruction_hashes_cover_unavailable_and_oversized_files(monkeypatch, tmp_path):
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setattr(automation_control, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        automation_control,
        "INSTRUCTION_FILES",
        (("missing", "missing.txt"), ("large", "large.txt")),
    )
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))

    hashes = service._instruction_hashes()
    templates = service.handle(_base_request(operation="templates"))

    assert hashes == [
        {"label": "missing", "path_label": "missing.txt", "sha256": "unavailable"},
        {"label": "large", "path_label": "large.txt", "sha256": "too_large"},
    ]
    assert templates["templates"][0]["template_id"] == "crm_digest_v1"


def test_readiness_reports_reconciliation_and_blocked_delivery(monkeypatch, tmp_path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    store.initialize()
    monkeypatch.setattr(
        AutomationStore,
        "technical_execution_state",
        lambda _self, **_kwargs: {
            "cursors": [],
            "runs": {"by_status": {}, "total_count": 0},
            "outbox": {"by_status": {"blocked": 1}, "total_count": 1, "blocked_count": 1},
        },
    )
    service = AutomationControlService(
        store,
        readiness_provider=lambda: {"ready": False, "checks": {"external_probe": "unavailable"}},
    )
    data = store.status()
    data["readiness"] = {"ready": True, "checks": {"registry": "ready"}}
    data["jobs"] = [
        "ignored",
        {
            "job_id": "auto_" + "1" * 24,
            "template_id": "crm_digest_v1",
            "desired_state": "on",
            "actual_state": "starting",
            "revision": 2,
            "applied_revision": 1,
            "reconcile_state": "applying",
            "lease_until": "2999-01-01T00:00:00Z",
        },
    ]
    data["system_timers"] = [
        "ignored",
        {
            "timer_id": "managed_pc_health",
            "desired_state": "on",
            "actual_state": "inactive",
            "revision": 1,
            "reconcile_state": "drift",
        },
    ]

    readiness = service._readiness_from_status(data)

    assert readiness["ready"] is False
    assert readiness["checks"] == {
        "registry": "ready",
        "external_probe": "unavailable",
        "job_reconciliation": "pending",
        "system_timer_reconciliation": "degraded",
        "outbox_delivery": "blocked",
    }
    assert readiness["execution_packet"]["jobs"][0]["lease"]["active"] is True


class PreviewTimerController:
    def __init__(self):
        self.rendered = []

    def inspect(self, timer_id):
        return {"timer_id": timer_id, "state": {"desired_state": "on"}}

    def render_interval_dropin(self, timer_id, *, every_minutes):
        self.rendered.append((timer_id, every_minutes))
        return "[Timer]"

    def list_status(self):
        return []


def test_preview_validates_timer_operations_and_global_hold(tmp_path):
    timers = PreviewTimerController()
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"), timer_controller=timers)

    invalid = [
        ({"target_operation": "set_enabled", "target_payload": []}, "automation_preview_payload_invalid"),
        (
            {"target_operation": "archive", "target_payload": {"timer_id": "managed_pc_health"}},
            "system_timer_operation_invalid",
        ),
        (
            {"target_operation": "set_enabled", "target_payload": {"timer_id": "unknown", "enabled": True}},
            "system_timer_not_allowed",
        ),
        (
            {
                "target_operation": "set_enabled",
                "target_payload": {"timer_id": "managed_pc_health", "enabled": 1},
            },
            "system_timer_enabled_invalid",
        ),
        (
            {
                "target_operation": "set_schedule",
                "target_payload": {"timer_id": "managed_pc_health", "schedule": []},
            },
            "system_timer_schedule_invalid",
        ),
    ]
    for payload, code in invalid:
        with pytest.raises(AutomationError, match=code):
            service._preview(payload)

    enabled = service._preview(
        {
            "target_operation": "set_enabled",
            "target_payload": {"timer_id": "managed_pc_health", "enabled": False},
        }
    )
    scheduled = service._preview(
        {
            "target_operation": "set_schedule",
            "target_payload": {"timer_id": "managed_pc_health", "schedule": {"every_minutes": 20}},
        }
    )
    hold = service._preview(
        {"target_operation": "set_global_hold", "target_payload": {"enabled": True, "reason": "release"}}
    )

    assert enabled["proposed"]["desired_state"] == "off"
    assert scheduled["proposed"]["period_minutes"] == 20
    assert timers.rendered == [("managed_pc_health", 20)]
    assert hold["proposed"] == {"enabled": True, "reason": "release"}


def _write_request(service, operation, payload, *, key, revision=None, actor_kind="codex"):
    request = _base_request(
        operation=operation,
        payload=payload,
        actor={"kind": actor_kind, "id": "control-test", "is_admin": True},
    )
    request["idempotency_key"] = key
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


def test_mutations_fail_closed_for_hold_unready_and_malformed_targets(tmp_path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    job = store.seed_defaults()["job"]
    service = AutomationControlService(store, readiness_provider=lambda: {"ready": False})

    with pytest.raises(AutomationError, match="automation_not_ready"):
        _write_request(
            service,
            "set_enabled",
            {"job_id": job["job_id"], "enabled": True},
            key="unready-enable-0001",
            revision=job["revision"],
        )
    with pytest.raises(AutomationError, match="automation_schedule_invalid"):
        _write_request(
            service,
            "set_schedule",
            {"job_id": job["job_id"], "schedule": []},
            key="invalid-schedule-0001",
            revision=job["revision"],
        )
    with pytest.raises(AutomationError, match="system_timer_operation_invalid"):
        _write_request(
            service,
            "archive",
            {"timer_id": "managed_pc_health", "job_id": job["job_id"]},
            key="invalid-timer-operation-0001",
            revision=job["revision"],
        )
    with pytest.raises(AutomationError, match="system_timer_not_allowed"):
        _write_request(
            service,
            "set_enabled",
            {"timer_id": "unknown", "enabled": False},
            key="unknown-timer-0001",
            revision=1,
        )

    hold = _write_request(
        service,
        "set_global_hold",
        {"enabled": True, "reason": "release", "attempt_hash": "a" * 64},
        key="enable-global-hold-0001",
        revision=0,
        actor_kind="system",
    )["global_hold"]
    assert hold["enabled"] is True
    with pytest.raises(AutomationError, match="automation_global_hold"):
        _write_request(
            service,
            "set_enabled",
            {"job_id": job["job_id"], "enabled": False},
            key="blocked-by-hold-0001",
            revision=job["revision"],
        )


class FakePeerSocket:
    def __init__(self, uid):
        self.uid = uid

    def getsockopt(self, *_args):
        return struct.pack("3i", 123, self.uid, 456)


class FakeReader:
    def __init__(self, raw):
        self.raw = raw

    async def readline(self):
        return self.raw


class FakeWriter:
    def __init__(self, *, peer_socket=None, drain_error=None):
        self.peer_socket = peer_socket
        self.drain_error = drain_error
        self.output = b""
        self.closed = False

    def get_extra_info(self, name):
        assert name == "socket"
        return self.peer_socket

    def write(self, data):
        self.output += data

    async def drain(self):
        if self.drain_error is not None:
            raise self.drain_error

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", "automation_request_invalid"),
        (b"not-json\n", "automation_request_invalid"),
        (b"[]\n", "automation_request_invalid"),
    ],
)
def test_server_rejects_malformed_frames(raw, expected, tmp_path):
    server = AutomationControlServer(
        socket_path=tmp_path / "control.sock",
        allowed_uids=frozenset({os.getuid()}),
    )
    writer = FakeWriter(peer_socket=FakePeerSocket(os.getuid()))

    asyncio.run(server.serve_connection(FakeReader(raw), writer))

    assert json.loads(writer.output)["error"]["code"] == expected
    assert writer.closed is True


def test_server_handles_missing_peer_internal_error_oversize_and_disconnect(monkeypatch, tmp_path):
    server = AutomationControlServer(
        socket_path=tmp_path / "control.sock",
        allowed_uids=frozenset({os.getuid()}),
    )
    missing_peer = FakeWriter()
    asyncio.run(server.serve_connection(FakeReader(b"{}\n"), missing_peer))
    assert json.loads(missing_peer.output)["error"]["code"] == "automation_peer_credentials_unavailable"

    request = _base_request()
    raw = json.dumps(request).encode() + b"\n"

    class BrokenService:
        def handle(self, _request):
            raise RuntimeError("private detail")

    server.service = BrokenService()
    internal = FakeWriter(peer_socket=FakePeerSocket(os.getuid()))
    asyncio.run(server.serve_connection(FakeReader(raw), internal))
    assert json.loads(internal.output)["error"]["code"] == "automation_internal_error"

    class LargeService:
        def handle(self, _request):
            return {"large": "x" * 1000}

    server.service = LargeService()
    monkeypatch.setattr(automation_control, "MAX_RESPONSE_BYTES", 300)
    oversized = FakeWriter(peer_socket=FakePeerSocket(os.getuid()), drain_error=ConnectionError())
    asyncio.run(server.serve_connection(FakeReader(raw), oversized))
    assert json.loads(oversized.output)["error"]["code"] == "automation_response_too_large"
    assert oversized.closed is True


def test_server_start_rejects_symlinked_directory_and_cleans_chmod_failure(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    unsafe = AutomationControlServer(
        socket_path=linked / "control.sock",
        allowed_uids=frozenset({os.getuid()}),
    )
    with pytest.raises(AutomationError, match="automation_socket_directory_invalid"):
        asyncio.run(unsafe.start())

    socket_path = tmp_path / "safe" / "control.sock"
    server = AutomationControlServer(
        socket_path=socket_path,
        allowed_uids=frozenset({os.getuid()}),
        socket_gid=os.getgid(),
    )
    real_chmod = automation_control.os.chmod

    def fail_socket_chmod(path, mode):
        if Path(path) == socket_path:
            raise OSError("chmod denied")
        return real_chmod(path, mode)

    monkeypatch.setattr(automation_control.os, "chmod", fail_socket_chmod)
    with pytest.raises(OSError, match="chmod denied"):
        asyncio.run(server.start())
    assert server.server is None
    assert not socket_path.exists()


class FakeClientSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _timeout):
        return None

    def connect(self, _path):
        return None

    def sendall(self, _payload):
        return None

    def recv(self, _size):
        return next(self.chunks, b"")


def test_client_rejects_oversized_requests_and_invalid_responses(monkeypatch, tmp_path):
    client = AutomationControlClient(socket_path=tmp_path / "control.sock")
    monkeypatch.setattr(automation_control, "MAX_REQUEST_BYTES", 100)
    with pytest.raises(AutomationError, match="automation_request_too_large"):
        client.request("status", {"large": "x" * 100})

    monkeypatch.setattr(automation_control, "MAX_REQUEST_BYTES", 64 * 1024)
    for chunks, code in [
        ([b""], "automation_response_invalid"),
        ([b"not-json\n"], "automation_response_invalid"),
        ([json.dumps({"ok": True}).encode() + b"\n"], "automation_response_invalid"),
    ]:
        monkeypatch.setattr(automation_control.socket, "socket", lambda *_args, value=chunks: FakeClientSocket(value))
        with pytest.raises(AutomationError, match=code):
            client.request("status")

    monkeypatch.setattr(automation_control, "MAX_RESPONSE_BYTES", 10)
    monkeypatch.setattr(
        automation_control.socket,
        "socket",
        lambda *_args: FakeClientSocket([b"x" * 11]),
    )
    with pytest.raises(AutomationError, match="automation_response_too_large"):
        client.request("status")


def test_control_cli_returns_success_or_safe_failure(monkeypatch, capsys):
    class Client:
        def request(self, operation, payload, *, idempotency_key, expected_revision):
            assert operation == "status"
            assert payload == {"job_id": "job"}
            assert idempotency_key == "request-key"
            assert expected_revision == 4
            return {"ok": True, "data": {}}

    monkeypatch.setattr(automation_control, "AutomationControlClient", Client)
    assert (
        automation_control.main(
            [
                "status",
                "--payload",
                '{"job_id":"job"}',
                "--idempotency-key",
                "request-key",
                "--expected-revision",
                "4",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert automation_control.main(["status", "--payload", "[]"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "automation_control_failed"
