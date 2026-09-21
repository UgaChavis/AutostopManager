from __future__ import annotations

import asyncio
import copy
import os
import re
from collections.abc import Mapping
from typing import Any

from autostop_manager.automation_control import (
    AutomationControlClient,
    AutomationControlServer,
    AutomationControlService,
)
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.telegram_automation_control import (
    TelegramAutomationAdapter,
    TelegramAutomationMessage,
    build_runtime_owner_adapter,
)


OWNER_PEER_ID = 9988776655


def _job(
    job_id: str = "auto_0123456789abcdef01234567",
    *,
    name: str = "CRM: краткий дайджест изменений",
    desired_state: str = "off",
    revision: int = 3,
    every_minutes: int = 20,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "template_id": "crm_digest_v1",
        "name": name,
        "desired_state": desired_state,
        "actual_state": "disabled" if desired_state == "off" else "idle",
        "revision": revision,
        "schedule": {"kind": "interval", "every_minutes": every_minutes, "timezone": "UTC"},
        "next_run_at": None if desired_state == "off" else "2026-09-21T12:20:00Z",
    }


class FakeControl:
    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs = copy.deepcopy(jobs if jobs is not None else [_job()])
        self.templates = [
            {
                "template_id": "crm_digest_v1",
                "name": "CRM: краткий дайджест изменений",
                "default_enabled": False,
            }
        ]
        self.calls: list[dict[str, Any]] = []
        self.idempotency: dict[str, dict[str, Any]] = {}

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        request = {
            "operation": operation,
            "payload": copy.deepcopy(dict(payload or {})),
            "idempotency_key": idempotency_key,
            "expected_revision": expected_revision,
        }
        self.calls.append(request)
        if operation == "status":
            job_id = request["payload"].get("job_id")
            jobs = [job for job in self.jobs if not job_id or job["job_id"] == job_id]
            return {"ok": True, "data": {"jobs": copy.deepcopy(jobs)}}
        if operation == "templates":
            return {"ok": True, "data": {"templates": copy.deepcopy(self.templates)}}
        if operation == "preview":
            return {"ok": True, "data": {"would_change": True}}
        if idempotency_key in self.idempotency:
            return copy.deepcopy(self.idempotency[str(idempotency_key)])
        if operation == "set_enabled":
            job = self._find_job(str(request["payload"]["job_id"]))
            if job["revision"] != expected_revision:
                return {"ok": False, "error": {"code": "automation_revision_conflict"}}
            job["desired_state"] = "on" if request["payload"]["enabled"] else "off"
            job["actual_state"] = "idle" if request["payload"]["enabled"] else "disabled"
            job["next_run_at"] = "2026-09-21T12:20:00Z" if request["payload"]["enabled"] else None
            job["revision"] += 1
            response = {"ok": True, "data": {"changed": True, "job": copy.deepcopy(job)}}
        elif operation == "set_schedule":
            job = self._find_job(str(request["payload"]["job_id"]))
            if job["revision"] != expected_revision:
                return {"ok": False, "error": {"code": "automation_revision_conflict"}}
            job["schedule"] = copy.deepcopy(request["payload"]["schedule"])
            job["revision"] += 1
            response = {"ok": True, "data": {"changed": True, "job": copy.deepcopy(job)}}
        elif operation == "create_from_template":
            schedule = request["payload"].get("schedule") or {
                "kind": "interval",
                "every_minutes": 20,
                "timezone": "UTC",
            }
            job = _job("auto_fedcba9876543210fedcba98", revision=1, every_minutes=schedule["every_minutes"])
            self.jobs.append(job)
            response = {"ok": True, "data": {"created": True, "job": copy.deepcopy(job)}}
        else:
            return {"ok": False, "error": {"code": "automation_operation_invalid"}}
        if idempotency_key is not None:
            self.idempotency[idempotency_key] = copy.deepcopy(response)
        return response

    def _find_job(self, job_id: str) -> dict[str, Any]:
        return next(job for job in self.jobs if job["job_id"] == job_id)


def _message(text: str, *, peer_id: int = OWNER_PEER_ID, message_id: int = 42, private: bool = True):
    return TelegramAutomationMessage(peer_id=peer_id, message_id=message_id, text=text, is_private=private)


def _confirmation_token(reply_text: str) -> str:
    match = re.search(r"Подтвердить ([A-F0-9]{8})", reply_text)
    assert match is not None
    return match.group(1)


def test_foreign_or_non_private_messages_are_ignored_without_control_calls():
    control = FakeControl()
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    foreign = adapter.handle(_message("включи CRM: краткий дайджест изменений", peer_id=123))
    group = adapter.handle(_message("включи CRM: краткий дайджест изменений", private=False))

    assert foreign.handled is False
    assert group.handled is False
    assert control.calls == []


def test_exact_on_command_applies_immediately_and_verifies_readback():
    control = FakeControl()
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    result = adapter.handle(_message("включи CRM: краткий дайджест изменений"))

    assert result.handled is True
    assert result.ok is True
    assert "ON" in result.reply_text
    assert [call["operation"] for call in control.calls] == ["status", "set_enabled", "status"]
    mutation = control.calls[1]
    assert mutation["payload"] == {"job_id": _job()["job_id"], "enabled": True}
    assert mutation["expected_revision"] == 3
    assert re.fullmatch(r"tg-auto:[0-9a-f]{40}", str(mutation["idempotency_key"]))
    assert str(OWNER_PEER_ID) not in str(mutation["idempotency_key"])
    assert control.jobs[0]["desired_state"] == "on"


def test_duplicate_on_command_reuses_idempotency_key_and_readback():
    control = FakeControl()
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)
    message = _message("включи CRM: краткий дайджест изменений")

    first = adapter.handle(message)
    second = adapter.handle(message)

    mutations = [call for call in control.calls if call["operation"] == "set_enabled"]
    assert first.ok is True
    assert second.ok is True
    assert mutations[0]["idempotency_key"] == mutations[1]["idempotency_key"]


def test_on_command_never_claims_success_when_exact_readback_mismatches():
    class WrongReadbackControl(FakeControl):
        def request(self, operation: str, payload=None, idempotency_key=None, expected_revision=None):
            response = super().request(operation, payload, idempotency_key, expected_revision)
            if operation == "status" and payload and payload.get("job_id") and self.jobs[0]["desired_state"] == "on":
                response["data"]["jobs"][0]["job_id"] = "auto_aaaaaaaaaaaaaaaaaaaaaaaa"
            return response

    control = WrongReadbackControl()
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    result = adapter.handle(_message("включи CRM: краткий дайджест изменений"))

    assert result.ok is False
    assert "не подтверждено" in result.reply_text
    assert len([call for call in control.calls if call["operation"] == "set_enabled"]) == 1


def test_ambiguous_job_name_never_mutates():
    jobs = [_job(), _job("auto_aaaaaaaaaaaaaaaaaaaaaaaa")]
    control = FakeControl(jobs)
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    result = adapter.handle(_message("выключи CRM: краткий дайджест изменений"))

    assert result.handled is True
    assert result.ok is False
    assert "неоднозначно" in result.reply_text
    assert [call["operation"] for call in control.calls] == ["status"]


def test_schedule_requires_preview_and_matching_confirmation_then_readback():
    control = FakeControl()
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    preview = adapter.handle(_message("поставь CRM: краткий дайджест изменений каждые 30 минут"))
    token = _confirmation_token(preview.reply_text)

    assert preview.ok is True
    assert [call["operation"] for call in control.calls] == ["status", "preview"]
    confirmed = adapter.handle(_message(f"Подтвердить {token}", message_id=43))

    assert confirmed.ok is True
    assert "30 мин" in confirmed.reply_text
    assert [call["operation"] for call in control.calls] == ["status", "preview", "set_schedule", "status"]
    assert control.calls[2]["expected_revision"] == 3
    assert control.jobs[0]["schedule"]["every_minutes"] == 30


def test_create_requires_preview_and_stays_off_after_confirmation():
    control = FakeControl(jobs=[])
    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=control)

    preview = adapter.handle(_message("добавь CRM: краткий дайджест изменений каждые 20 минут"))
    token = _confirmation_token(preview.reply_text)

    assert [call["operation"] for call in control.calls] == ["templates", "preview"]
    confirmed = adapter.handle(_message(f"Подтвердить {token}", message_id=44))

    assert confirmed.ok is True
    assert "OFF" in confirmed.reply_text
    assert control.calls[2]["operation"] == "create_from_template"
    assert control.calls[2]["payload"]["enabled"] is False
    assert control.jobs[0]["desired_state"] == "off"


def test_expired_confirmation_and_arbitrary_text_never_mutate():
    clock = [100.0]
    control = FakeControl()
    adapter = TelegramAutomationAdapter(
        owner_peer_id=OWNER_PEER_ID,
        control=control,
        confirmation_ttl_seconds=30,
        now=lambda: clock[0],
    )
    preview = adapter.handle(_message("период CRM: краткий дайджест изменений раз в 1 час"))
    token = _confirmation_token(preview.reply_text)
    clock[0] = 131.0

    expired = adapter.handle(_message(f"Подтвердить {token}", message_id=45))
    arbitrary = adapter.handle(_message("выполни rm -rf /", message_id=46))

    assert expired.ok is False
    assert arbitrary.handled is False
    assert not any(call["operation"] == "set_schedule" for call in control.calls)


def test_control_exception_is_returned_as_safe_failure():
    class FailingControl:
        def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AutomationError("private_backend_detail")

    adapter = TelegramAutomationAdapter(owner_peer_id=OWNER_PEER_ID, control=FailingControl())

    result = adapter.handle(_message("регламентные задания"))

    assert result.handled is True
    assert result.ok is False
    assert "automation_control_unavailable" in result.reply_text
    assert "private_backend_detail" not in result.reply_text


def test_adapter_uses_real_local_control_protocol_and_exact_readback(tmp_path):
    async def scenario():
        socket_path = tmp_path / "automation.sock"
        server = AutomationControlServer(
            service=AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3")),
            socket_path=socket_path,
            allowed_uids=frozenset({os.getuid()}),
        )
        await server.start()
        try:
            bootstrap = AutomationControlClient(socket_path=socket_path)
            created = await asyncio.to_thread(
                bootstrap.request,
                "create_from_template",
                {"template_id": "crm_digest_v1", "enabled": False},
                "test-create-crm-digest",
            )
            assert created["ok"] is True
            job_id = created["data"]["job"]["job_id"]
            adapter = build_runtime_owner_adapter(owner_peer_id=OWNER_PEER_ID, socket_path=socket_path)
            result = await asyncio.to_thread(adapter.handle, _message(f"включи {job_id}"))
            readback = await asyncio.to_thread(bootstrap.request, "status", {"job_id": job_id})
            return result, readback
        finally:
            await server.close()

    result, readback = asyncio.run(scenario())

    assert result.ok is True
    assert readback["data"]["jobs"][0]["desired_state"] == "on"
    assert str(OWNER_PEER_ID).encode() not in (tmp_path / "manager.sqlite3").read_bytes()
