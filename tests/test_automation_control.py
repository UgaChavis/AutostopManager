from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from pathlib import Path
from uuid import uuid4

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
