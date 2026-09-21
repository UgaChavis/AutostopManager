"""Bounded local control protocol for Manager automations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from .automation_registry import (
    AUTOMATION_TEMPLATES,
    AutomationError,
    AutomationStore,
    canonical_json,
    parse_time,
    technical_actor_hash,
    utc_now,
)
from .automation_timers import SYSTEM_TIMER_ALLOWLIST, SystemTimerController
from .config import (
    PROJECT_ROOT,
    get_automation_control_allowed_uids,
    get_automation_control_gid,
    get_automation_control_peer_roles,
    get_automation_control_socket_path,
    get_automation_runtime_identity,
)


AUTOMATION_CONTROL_PROTOCOL = "autostop.manager.automation-control.v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
READ_OPERATIONS = frozenset({"status", "templates", "readiness", "preview"})
WRITE_OPERATIONS = frozenset(
    {
        "create_from_template",
        "set_enabled",
        "set_schedule",
        "run_now",
        "test_notification",
        "archive",
        "set_global_hold",
    }
)
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
ACTOR_KINDS = frozenset({"crm_operator", "telegram_owner", "codex", "system"})
INSTRUCTION_FILES = (
    ("project_instructions", "AGENTS.md"),
    ("owner_telegram_instructions", ".agents/skills/manage-owner-telegram/SKILL.md"),
    ("store_instructions", ".agents/skills/manage-autostop-store/SKILL.md"),
    ("deployment_runbook", "docs/agent/deployment_runbook.md"),
)


def _required_text(payload: Mapping[str, Any], key: str, *, maximum: int = 128) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AutomationError(f"automation_{key}_invalid")
    clean = value.strip()
    if any(ord(char) < 32 for char in clean):
        raise AutomationError(f"automation_{key}_invalid")
    return clean


def _validate_actor(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value).difference({"kind", "id", "is_admin"}):
        raise AutomationError("automation_actor_invalid")
    kind = _required_text(value, "kind", maximum=32)
    actor_id = _required_text(value, "id", maximum=128)
    is_admin = value.get("is_admin", False)
    if kind not in ACTOR_KINDS or type(is_admin) is not bool:
        raise AutomationError("automation_actor_invalid")
    return {"kind": kind, "id": actor_id, "is_admin": is_admin}


def _can_manage(actor: Mapping[str, Any]) -> bool:
    return bool(actor["kind"] in {"codex", "telegram_owner", "system"} or actor["is_admin"])


def _expected_revision(request: Mapping[str, Any]) -> int | None:
    value = request.get("expected_revision")
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise AutomationError("expected_revision_invalid")
    return value


class AutomationControlService:
    """Validate one command and apply it transactionally to the registry."""

    def __init__(
        self,
        store: AutomationStore | None = None,
        *,
        timer_controller: SystemTimerController | None = None,
        readiness_provider: Any | None = None,
    ) -> None:
        self.store = store or AutomationStore()
        self.timer_controller = timer_controller or SystemTimerController(store=self.store)
        self.readiness_provider = readiness_provider

    @staticmethod
    def _instruction_hashes() -> list[dict[str, str]]:
        results = []
        for label, relative_path in INSTRUCTION_FILES:
            path = PROJECT_ROOT / relative_path
            try:
                content = path.read_bytes()
            except OSError:
                digest = "unavailable"
            else:
                digest = hashlib.sha256(content).hexdigest() if len(content) <= 1024 * 1024 else "too_large"
            results.append({"label": label, "path_label": relative_path, "sha256": digest})
        return results

    def _timer_statuses(self, persisted_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        persisted = {str(item["timer_id"]): item for item in persisted_items}
        timers = []
        for live in self.timer_controller.list_status():
            saved = persisted.get(str(live["timer_id"]), {})
            raw_state = live.get("state")
            state: Mapping[str, Any] = raw_state if isinstance(raw_state, Mapping) else {}
            desired_state = saved.get("desired_state", state.get("desired_state", "off"))
            desired_period = saved.get("period_minutes", state.get("period_minutes"))
            actual_state = state.get("actual_state", "unknown")
            expected_actual = "active" if desired_state == "on" else "inactive"
            inspection_error = None if live.get("inspection_ok") else "system_timer_inspection_failed"
            drift = bool(
                live.get("inspection_ok")
                and (
                    actual_state != expected_actual
                    or (
                        desired_period is not None
                        and state.get("period_minutes") is not None
                        and desired_period != state.get("period_minutes")
                    )
                )
            )
            timers.append(
                {
                    "timer_id": live["timer_id"],
                    "name": live["name"],
                    "control_mode": live["control_mode"],
                    "locked": live["locked"],
                    "desired_state": desired_state,
                    "actual_state": actual_state,
                    "period_minutes": desired_period,
                    "actual_period_minutes": state.get("period_minutes"),
                    "revision": int(saved.get("revision", 0)),
                    "next_run_at": state.get("next_run_at"),
                    "last_run_at": state.get("last_run_at"),
                    "reconcile_state": "inspection_error" if inspection_error else ("drift" if drift else "in_sync"),
                    "error_code": state.get("error_code")
                    or inspection_error
                    or ("system_timer_drift" if drift else None),
                }
            )
        return timers

    def _execution_packet(self, data: Mapping[str, Any], checks: Mapping[str, Any]) -> dict[str, Any]:
        identity = get_automation_runtime_identity()
        now = utc_now()
        jobs = []
        for raw_job in data.get("jobs", []):
            if not isinstance(raw_job, Mapping):
                continue
            lease_until = parse_time(raw_job.get("lease_until"))
            jobs.append(
                {
                    "job_id": raw_job.get("job_id"),
                    "template_id": raw_job.get("template_id"),
                    "desired_state": raw_job.get("desired_state"),
                    "actual_state": raw_job.get("actual_state"),
                    "revision": raw_job.get("revision"),
                    "applied_revision": raw_job.get("applied_revision"),
                    "reconcile_state": raw_job.get("reconcile_state"),
                    "lag_seconds": raw_job.get("lag_seconds"),
                    "lease": {
                        "active": bool(lease_until is not None and lease_until > now),
                        "until": raw_job.get("lease_until"),
                    },
                    "fencing_token": raw_job.get("fencing_token"),
                    "error_code": raw_job.get("error_code"),
                    "next_run_at": raw_job.get("next_run_at"),
                }
            )
        timers = [
            {
                "timer_id": timer.get("timer_id"),
                "desired_state": timer.get("desired_state"),
                "actual_state": timer.get("actual_state"),
                "revision": timer.get("revision"),
                "reconcile_state": timer.get("reconcile_state"),
                "error_code": timer.get("error_code"),
            }
            for timer in data.get("system_timers", [])
            if isinstance(timer, Mapping)
        ]
        technical = self.store.technical_execution_state(job_id=(str(jobs[0]["job_id"]) if len(jobs) == 1 else None))
        return {
            "format": "manager_automation_execution_packet_v1",
            "generated_at": data.get("generated_at"),
            "manager_revision": identity["manager_revision"],
            "crm": {
                "version": identity["crm_version"],
                "revision": identity["crm_revision"],
            },
            "instruction_hashes": self._instruction_hashes(),
            "dependencies": dict(checks),
            "controller": {
                **dict(data.get("controller", {})),
                "global_hold": dict(data.get("global_hold", {})),
            },
            "jobs": jobs,
            "system_timers": timers,
            **technical,
        }

    def _readiness_from_status(self, data: Mapping[str, Any]) -> dict[str, Any]:
        baseline = data.get("readiness", {})
        baseline = baseline if isinstance(baseline, Mapping) else {}
        provided: Mapping[str, Any] = {}
        if self.readiness_provider is not None:
            candidate = self.readiness_provider()
            if isinstance(candidate, Mapping):
                provided = candidate
        checks = {**dict(baseline.get("checks", {})), **dict(provided.get("checks", {}))}
        ready = bool(baseline.get("ready"))
        if self.readiness_provider is not None:
            ready = ready and bool(provided.get("ready"))
        ready = ready and not bool(dict(data.get("global_hold", {})).get("enabled"))
        packet = self._execution_packet(data, checks)
        if any(isinstance(job, Mapping) and job.get("reconcile_state") != "in_sync" for job in data.get("jobs", [])):
            checks["job_reconciliation"] = "pending"
            ready = False
        if any(
            isinstance(timer, Mapping) and timer.get("reconcile_state") != "in_sync"
            for timer in data.get("system_timers", [])
        ):
            checks["system_timer_reconciliation"] = "degraded"
            ready = False
        outbox = packet.get("outbox")
        if isinstance(outbox, Mapping) and int(outbox.get("blocked_count") or 0) > 0:
            checks["outbox_delivery"] = "blocked"
            ready = False
        packet["dependencies"] = dict(checks)
        return {
            "ready": ready,
            "checks": checks,
            "warnings": [key for key, value in checks.items() if value != "ready"],
            "execution_packet": packet,
        }

    def _readiness(self, *, job_id: str | None = None) -> dict[str, Any]:
        data = self.store.status(job_id=job_id)
        data["system_timers"] = self._timer_statuses(data["system_timers"])
        return self._readiness_from_status(data)

    def _status(self, *, job_id: str | None, include_archived: bool, can_manage: bool) -> dict[str, Any]:
        data = self.store.status(job_id=job_id, include_archived=include_archived)
        data["system_timers"] = self._timer_statuses(data["system_timers"])
        data["can_manage"] = can_manage
        data["readiness"] = self._readiness_from_status(data)
        return data

    def _preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_operation = str(payload.get("target_operation") or "")
        target_payload = payload.get("target_payload", {})
        if not isinstance(target_payload, Mapping):
            raise AutomationError("automation_preview_payload_invalid")
        timer_id = target_payload.get("timer_id")
        if timer_id is not None:
            if not isinstance(timer_id, str) or target_operation not in {"set_enabled", "set_schedule"}:
                raise AutomationError("system_timer_operation_invalid")
            policy = SYSTEM_TIMER_ALLOWLIST.get(timer_id)
            if policy is None:
                raise AutomationError("system_timer_not_allowed")
            current = self.timer_controller.inspect(timer_id)
            proposed: dict[str, Any] = {"timer_id": timer_id}
            if target_operation == "set_enabled":
                if type(target_payload.get("enabled")) is not bool:
                    raise AutomationError("system_timer_enabled_invalid")
                proposed["desired_state"] = "on" if target_payload["enabled"] else "off"
            else:
                schedule = target_payload.get("schedule")
                if not isinstance(schedule, Mapping) or type(schedule.get("every_minutes")) is not int:
                    raise AutomationError("system_timer_schedule_invalid")
                self.timer_controller.render_interval_dropin(timer_id, every_minutes=schedule["every_minutes"])
                proposed["period_minutes"] = schedule["every_minutes"]
            return {
                "target_operation": target_operation,
                "would_change": True,
                "allowed": policy.control_mode == "managed",
                "locked": policy.control_mode == "read_only",
                "current": current,
                "proposed": proposed,
                "external_effects": ["systemd_dropin_or_unit_state", "systemd_readback"],
            }
        if target_operation == "set_global_hold":
            status = self.store.status()
            return {
                "target_operation": target_operation,
                "would_change": status["global_hold"]["enabled"] != bool(target_payload.get("enabled")),
                "current": status["global_hold"],
                "proposed": {
                    "enabled": target_payload.get("enabled"),
                    "reason": target_payload.get("reason"),
                },
                "external_effects": [],
            }
        return self.store.preview(payload)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:  # noqa: C901
        allowed_keys = {
            "protocol",
            "request_id",
            "operation",
            "actor",
            "payload",
            "idempotency_key",
            "expected_revision",
        }
        if set(request).difference(allowed_keys):
            raise AutomationError("automation_request_fields_invalid")
        if request.get("protocol") != AUTOMATION_CONTROL_PROTOCOL:
            raise AutomationError("automation_protocol_invalid")
        request_id = _required_text(request, "request_id", maximum=64)
        try:
            UUID(request_id)
        except ValueError as exc:
            raise AutomationError("automation_request_id_invalid") from exc
        operation = _required_text(request, "operation", maximum=48)
        if operation not in OPERATIONS:
            raise AutomationError("automation_operation_invalid")
        actor = _validate_actor(request.get("actor"))
        payload = request.get("payload", {})
        if not isinstance(payload, Mapping):
            raise AutomationError("automation_payload_invalid")
        if len(canonical_json(payload).encode()) > 32 * 1024:
            raise AutomationError("automation_payload_too_large")
        expected_revision = _expected_revision(request)

        if operation in WRITE_OPERATIONS and not _can_manage(actor):
            raise AutomationError("automation_permission_denied")
        if (
            operation == "set_global_hold"
            or (operation == "preview" and payload.get("target_operation") == "set_global_hold")
        ) and actor["kind"] != "system":
            raise AutomationError("automation_permission_denied")
        if operation == "status":
            job_id = payload.get("job_id")
            if job_id is not None and not isinstance(job_id, str):
                raise AutomationError("automation_job_id_invalid")
            return self._status(
                job_id=job_id or None,
                include_archived=payload.get("include_archived") is True,
                can_manage=_can_manage(actor),
            )
        if operation == "templates":
            return {"templates": [template.public() for template in AUTOMATION_TEMPLATES.values()]}
        if operation == "readiness":
            job_id = payload.get("job_id")
            if job_id is not None and not isinstance(job_id, str):
                raise AutomationError("automation_job_id_invalid")
            return self._readiness(job_id=job_id or None)
        if operation == "preview":
            return self._preview(payload)

        idempotency_key = request.get("idempotency_key")
        if not isinstance(idempotency_key, str):
            raise AutomationError("idempotency_key_required")
        actor_hash = technical_actor_hash(str(actor["kind"]), str(actor["id"]))
        request_hash = technical_actor_hash(
            "automation-command",
            canonical_json(
                {
                    "operation": operation,
                    "payload": payload,
                    "expected_revision": expected_revision,
                    "actor_hash": actor_hash,
                }
            ),
        )

        def mutation(connection: Any, command_id: str) -> dict[str, Any]:
            if operation == "create_from_template":
                template_id = _required_text(payload, "template_id", maximum=64)
                return self.store.create_from_template(
                    connection,
                    template_id=template_id,
                    payload=payload,
                    actor_hash=actor_hash,
                )
            if operation == "set_global_hold":
                return self.store.set_global_hold(
                    connection,
                    enabled=cast(bool, payload.get("enabled")),
                    reason=payload.get("reason"),
                    actor_hash=actor_hash,
                    attempt_hash=payload.get("attempt_hash"),
                    expected_revision=expected_revision,
                )
            controller = connection.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton = 1"
            ).fetchone()
            if controller is not None and bool(controller["hold_enabled"]):
                raise AutomationError("automation_global_hold")
            timer_id = payload.get("timer_id")
            if timer_id is not None:
                if operation not in {"set_enabled", "set_schedule"} or not isinstance(timer_id, str):
                    raise AutomationError("system_timer_operation_invalid")
                policy = SYSTEM_TIMER_ALLOWLIST.get(timer_id)
                if policy is None:
                    raise AutomationError("system_timer_not_allowed")
                current = self.store.require_system_timer_revision(
                    connection,
                    timer_id=timer_id,
                    expected_revision=expected_revision,
                )
                if policy.control_mode != "managed":
                    raise AutomationError("system_timer_read_only")
                if operation == "set_enabled":
                    live = self.timer_controller.set_enabled(timer_id, enabled=cast(bool, payload.get("enabled")))
                else:
                    schedule = payload.get("schedule")
                    if not isinstance(schedule, Mapping) or type(schedule.get("every_minutes")) is not int:
                        raise AutomationError("system_timer_schedule_invalid")
                    live = self.timer_controller.set_schedule(
                        timer_id,
                        every_minutes=schedule["every_minutes"],
                        expected_dropin_sha256=current.get("dropin_sha256"),
                    )
                state = dict(live.get("state", {}))
                if isinstance(live.get("dropin_sha256"), str):
                    state["dropin_sha256"] = live["dropin_sha256"]
                updated = self.store.update_system_timer(
                    connection,
                    timer_id=timer_id,
                    unit_name=policy.unit_name,
                    control_mode=policy.control_mode,
                    state=state,
                    expected_revision=expected_revision,
                )
                updated["name"] = policy.display_name
                return {"changed": True, "system_timer": updated}
            job_id = _required_text(payload, "job_id", maximum=64)
            if operation == "set_enabled":
                if payload.get("enabled") is True and self.readiness_provider is not None:
                    readiness = self.readiness_provider()
                    if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
                        raise AutomationError("automation_not_ready")
                return self.store.set_enabled(
                    connection,
                    job_id=job_id,
                    enabled=cast(bool, payload.get("enabled")),
                    expected_revision=expected_revision,
                )
            if operation == "set_schedule":
                schedule = payload.get("schedule")
                if not isinstance(schedule, Mapping):
                    raise AutomationError("automation_schedule_invalid")
                return self.store.set_schedule(
                    connection,
                    job_id=job_id,
                    schedule_payload=schedule,
                    expected_revision=expected_revision,
                )
            if operation == "run_now":
                return self.store.enqueue_run(
                    connection,
                    job_id=job_id,
                    command_id=command_id,
                    expected_revision=expected_revision,
                )
            if operation == "test_notification":
                return self.store.enqueue_notification_test(
                    connection,
                    job_id=job_id,
                    command_id=command_id,
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                )
            if operation == "archive":
                return self.store.archive(
                    connection,
                    job_id=job_id,
                    expected_revision=expected_revision,
                )
            raise AutomationError("automation_operation_invalid")

        return self.store.execute_command(
            source=str(actor["kind"]),
            idempotency_key=idempotency_key,
            operation=operation,
            request_hash=request_hash,
            actor_hash=actor_hash,
            mutation=mutation,
        )


def _response(
    request_id: str, *, data: Mapping[str, Any] | None = None, error: AutomationError | None = None
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": request_id,
        "ok": error is None,
    }
    if error is None:
        response["data"] = dict(data or {})
    else:
        response["error"] = {"code": error.code, "message": error.code, **error.details}
    return response


class AutomationControlServer:
    def __init__(
        self,
        *,
        service: AutomationControlService | None = None,
        socket_path: Path | None = None,
        allowed_uids: frozenset[int] | None = None,
        peer_roles: Mapping[int, frozenset[str]] | None = None,
        socket_gid: int | None = None,
    ) -> None:
        self.service = service or AutomationControlService()
        self.socket_path = socket_path or get_automation_control_socket_path()
        if peer_roles is not None:
            self.peer_roles = {int(uid): frozenset(roles) for uid, roles in peer_roles.items()}
        elif allowed_uids is not None:
            # Explicit constructor use is primarily for isolated tests.  The
            # production daemon always loads a UID-to-role mapping from config.
            self.peer_roles = dict.fromkeys(allowed_uids, ACTOR_KINDS)
        else:
            self.peer_roles = get_automation_control_peer_roles()
        self.allowed_uids = frozenset(self.peer_roles) or get_automation_control_allowed_uids()
        self.socket_gid = get_automation_control_gid() if socket_gid is None else socket_gid
        self.server: asyncio.AbstractServer | None = None

    async def serve_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id = ""
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None:
                raise AutomationError("automation_peer_credentials_unavailable")
            _pid, uid, _gid = struct.unpack(
                "3i",
                peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw or not raw.endswith(b"\n") or len(raw) > MAX_REQUEST_BYTES:
                raise AutomationError("automation_request_invalid")
            try:
                request = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AutomationError("automation_request_invalid") from exc
            if not isinstance(request, dict):
                raise AutomationError("automation_request_invalid")
            raw_request_id = request.get("request_id")
            request_id = raw_request_id if isinstance(raw_request_id, str) and len(raw_request_id) <= 64 else ""
            if uid not in self.allowed_uids:
                raise AutomationError("automation_peer_not_allowed")
            raw_actor = request.get("actor")
            actor_kind = raw_actor.get("kind") if isinstance(raw_actor, Mapping) else None
            if actor_kind not in self.peer_roles.get(uid, frozenset()):
                raise AutomationError("automation_peer_role_not_allowed")
            data = await asyncio.to_thread(self.service.handle, request)
            response = _response(request_id, data=data)
        except (AutomationError, TimeoutError, ValueError, OSError) as exc:
            error = exc if isinstance(exc, AutomationError) else AutomationError("automation_request_failed")
            response = _response(request_id, error=error)
        except Exception:  # noqa: BLE001 - never return private exception text over the socket.
            response = _response(request_id, error=AutomationError("automation_internal_error"))
        encoded = canonical_json(response).encode() + b"\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (
                canonical_json(_response(request_id, error=AutomationError("automation_response_too_large"))).encode()
                + b"\n"
            )
        try:
            writer.write(encoded)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def start(self) -> asyncio.AbstractServer:
        parent = self.socket_path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            raise AutomationError("automation_socket_directory_invalid")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(parent, directory_flags)
        try:
            current = os.fstat(directory_fd)
            if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid():
                raise AutomationError("automation_socket_directory_invalid")
            if self.socket_gid is not None:
                os.fchown(directory_fd, -1, self.socket_gid)
            os.fchmod(directory_fd, 0o750)
        finally:
            os.close(directory_fd)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                raise AutomationError("automation_socket_path_unsafe")
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self.serve_connection,
            path=str(self.socket_path),
            limit=MAX_REQUEST_BYTES,
        )
        try:
            if self.socket_gid is not None:
                os.chown(self.socket_path, -1, self.socket_gid)
            os.chmod(self.socket_path, 0o660)
        except OSError:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            with suppress(FileNotFoundError):
                self.socket_path.unlink()
            raise
        return self.server

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        with suppress(FileNotFoundError):
            info = self.socket_path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                self.socket_path.unlink()


class AutomationControlClient:
    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        actor: Mapping[str, Any] | None = None,
        timeout: float = 10,
    ) -> None:
        self.socket_path = socket_path or get_automation_control_socket_path()
        self.actor = dict(actor or {"kind": "codex", "id": "manager-client", "is_admin": True})
        self.timeout = timeout

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid4())
        request: dict[str, Any] = {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": request_id,
            "operation": operation,
            "actor": self.actor,
            "payload": dict(payload or {}),
        }
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        if expected_revision is not None:
            request["expected_revision"] = expected_revision
        encoded = canonical_json(request).encode() + b"\n"
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AutomationError("automation_request_too_large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(encoded)
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = client.recv(min(8192, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise AutomationError("automation_response_too_large")
        line = bytes(chunks).split(b"\n", 1)[0]
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AutomationError("automation_response_invalid") from exc
        if (
            not isinstance(response, dict)
            or response.get("protocol") != AUTOMATION_CONTROL_PROTOCOL
            or response.get("request_id") != request_id
            or type(response.get("ok")) is not bool
        ):
            raise AutomationError("automation_response_invalid")
        return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("--payload", default="{}")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError
        response = AutomationControlClient().request(
            args.operation,
            payload,
            idempotency_key=args.idempotency_key,
            expected_revision=args.expected_revision,
        )
    except (AutomationError, OSError, TimeoutError, ValueError):
        response = {"ok": False, "error": {"code": "automation_control_failed"}}
    print(canonical_json(response))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
