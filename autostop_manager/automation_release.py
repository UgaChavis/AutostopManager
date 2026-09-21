"""Machine-readable release preflight for the Automation Center scheduler."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .automation_control import (
    AUTOMATION_CONTROL_PROTOCOL,
    AutomationControlClient,
    AutomationControlService,
)
from .automation_registry import (
    AUTOMATION_SCHEMA_VERSION,
    AutomationError,
    AutomationStore,
    canonical_json,
    parse_time,
    utc_now,
)
from .automation_timers import SYSTEM_TIMER_ALLOWLIST, SystemTimerController
from .config import get_automation_control_socket_path, load_runtime_env


def _command_key(prefix: str, release_key: str) -> str:
    digest = hashlib.sha256(release_key.encode()).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _attempt_hash(release_attempt_key: str) -> str:
    return hashlib.sha256(f"release-attempt:{release_attempt_key}".encode()).hexdigest()


def _local_request(
    service: AutomationControlService,
    operation: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "system", "id": "release-preflight", "is_admin": True},
        "payload": payload,
        "idempotency_key": idempotency_key,
    }
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    return service.handle(request)


def _socket_available(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode):
        raise AutomationError("automation_socket_path_unsafe")
    return True


RELEASE_ATTEMPT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")


def _release_attempt_key(value: str) -> str:
    if RELEASE_ATTEMPT_PATTERN.fullmatch(value) is None:
        raise AutomationError("automation_release_attempt_key_invalid")
    return value


def _socket_client(path: Path) -> AutomationControlClient:
    return AutomationControlClient(
        socket_path=path,
        actor={"kind": "system", "id": "release-preflight", "is_admin": True},
    )


def _client_status(client: AutomationControlClient) -> dict[str, Any]:
    response = client.request("status", {"include_archived": True})
    if response.get("ok") is not True or not isinstance(response.get("data"), dict):
        raise AutomationError("automation_release_status_failed")
    return dict(response["data"])


def _require_quiescent(status: dict[str, Any]) -> None:
    now = utc_now()
    for item in status.get("jobs", []):
        if not isinstance(item, dict):
            raise AutomationError("automation_release_status_invalid")
        lease_until = parse_time(item.get("lease_until"))
        if lease_until is not None and lease_until > now:
            raise AutomationError("automation_release_not_quiescent")
    readiness = status.get("readiness")
    packet = readiness.get("execution_packet") if isinstance(readiness, dict) else None
    outbox = packet.get("outbox") if isinstance(packet, dict) else None
    if not isinstance(outbox, dict) or not isinstance(outbox.get("by_status"), dict):
        raise AutomationError("automation_release_quiescence_unavailable")
    if int(outbox["by_status"].get("sending", 0)) != 0:
        raise AutomationError("automation_release_not_quiescent")


def _wait_quiescent(client: AutomationControlClient, initial: dict[str, Any]) -> dict[str, Any]:
    status = initial
    for attempt in range(50):
        try:
            _require_quiescent(status)
        except AutomationError as exc:
            if exc.code != "automation_release_not_quiescent" or attempt == 49:
                raise
            time.sleep(0.1)
            status = _client_status(client)
        else:
            return status
    raise AutomationError("automation_release_not_quiescent")


def _require_timer_readback(status: dict[str, Any]) -> None:
    timers = status.get("system_timers")
    if not isinstance(timers, list):
        raise AutomationError("automation_release_timer_readback_failed")
    by_id = {
        str(item.get("timer_id")): item
        for item in timers
        if isinstance(item, dict) and isinstance(item.get("timer_id"), str)
    }
    if set(by_id) != set(SYSTEM_TIMER_ALLOWLIST):
        raise AutomationError("automation_release_timer_readback_failed")
    if any(
        type(item.get("revision")) is not int
        or int(item["revision"]) < 1
        or item.get("error_code") is not None
        or item.get("reconcile_state") != "in_sync"
        for item in by_id.values()
    ):
        raise AutomationError("automation_release_timer_readback_failed")


def hold(*, release_attempt_key: str) -> dict[str, Any]:
    release_attempt_key = _release_attempt_key(release_attempt_key)
    attempt_hash = _attempt_hash(release_attempt_key)
    socket_path = get_automation_control_socket_path()
    hold_key = _command_key("release-hold", release_attempt_key)
    if _socket_available(socket_path):
        client = _socket_client(socket_path)
        status = _client_status(client)
        hold_state = status.get("global_hold")
        if not isinstance(hold_state, dict) or type(hold_state.get("revision")) is not int:
            raise AutomationError("automation_release_hold_state_invalid")
        if hold_state.get("enabled") is True and hold_state.get("reason") != "release":
            raise AutomationError("automation_release_preexisting_hold")
        if hold_state.get("enabled") is not True:
            response = client.request(
                "set_global_hold",
                {"enabled": True, "reason": "release", "attempt_hash": attempt_hash},
                idempotency_key=hold_key,
                expected_revision=int(hold_state["revision"]),
            )
            if response.get("ok") is not True:
                raise AutomationError("automation_release_hold_failed")
        readback = _client_status(client)
        hold_state = readback.get("global_hold")
        if (
            not isinstance(hold_state, dict)
            or hold_state.get("enabled") is not True
            or hold_state.get("reason") != "release"
            or hold_state.get("attempt_hash") != attempt_hash
        ):
            raise AutomationError("automation_release_hold_readback_failed")
        readback = _wait_quiescent(client, readback)
        hold_state = readback["global_hold"]
        return {"ok": True, "mode": "socket", "quiescent": True, "global_hold": hold_state}

    store = AutomationStore()
    if store.path.exists():
        with store.connect() as connection:
            schema = connection.execute(
                "SELECT version FROM manager_automation_schema WHERE component = 'automation_center'"
            ).fetchone()
        if schema is None or int(schema["version"]) != AUTOMATION_SCHEMA_VERSION:
            raise AutomationError("automation_release_backup_required_before_migration")
    store.initialize()
    status = store.status()
    hold_state = status["global_hold"]
    if hold_state["enabled"] and hold_state["reason"] != "release":
        raise AutomationError("automation_release_preexisting_hold")
    if not hold_state["enabled"]:
        service = AutomationControlService(store)
        hold_result = _local_request(
            service,
            "set_global_hold",
            {"enabled": True, "reason": "release", "attempt_hash": attempt_hash},
            idempotency_key=hold_key,
            expected_revision=int(hold_state["revision"]),
        )
        hold_state = hold_result["global_hold"]
    readback = store.status()
    if (
        not readback["global_hold"]["enabled"]
        or readback["global_hold"]["reason"] != "release"
        or readback["global_hold"].get("attempt_hash") != attempt_hash
    ):
        raise AutomationError("automation_release_hold_readback_failed")
    technical = store.technical_execution_state()
    if technical["outbox"]["by_status"].get("sending", 0):
        raise AutomationError("automation_release_not_quiescent")
    now = utc_now()
    if any((lease := parse_time(item.get("lease_until"))) is not None and lease > now for item in readback["jobs"]):
        raise AutomationError("automation_release_not_quiescent")
    return {
        "ok": True,
        "mode": "offline",
        "quiescent": True,
        "global_hold": hold_state,
    }


def seed(*, release_attempt_key: str) -> dict[str, Any]:
    release_attempt_key = _release_attempt_key(release_attempt_key)
    attempt_hash = _attempt_hash(release_attempt_key)
    socket_path = get_automation_control_socket_path()
    seed_key = _command_key("release-seed", release_attempt_key)
    if _socket_available(socket_path):
        client = _socket_client(socket_path)
        status = _client_status(client)
        hold_state = status.get("global_hold")
        if (
            not isinstance(hold_state, dict)
            or hold_state.get("enabled") is not True
            or hold_state.get("reason") != "release"
            or hold_state.get("attempt_hash") != attempt_hash
        ):
            raise AutomationError("automation_release_hold_required")
        jobs = status.get("jobs") if isinstance(status.get("jobs"), list) else []
        existing = [item for item in jobs if isinstance(item, dict) and item.get("template_id") == "crm_digest_v1"]
        if not existing:
            response = client.request(
                "create_from_template",
                {"template_id": "crm_digest_v1", "enabled": False},
                idempotency_key=seed_key,
            )
            if response.get("ok") is not True:
                raise AutomationError("automation_release_seed_failed")
            seeded = True
        else:
            seeded = False
        readback = _client_status(client)
        jobs = [item for item in readback.get("jobs", []) if isinstance(item, dict)]
        _require_timer_readback(readback)
        timers_verified = True
    else:
        store = AutomationStore()
        status = store.status()
        if (
            status["global_hold"]["enabled"] is not True
            or status["global_hold"]["reason"] != "release"
            or status["global_hold"].get("attempt_hash") != attempt_hash
        ):
            raise AutomationError("automation_release_hold_required")
        result = store.seed_defaults()
        seeded = bool(result.get("seeded")) and result.get("idempotent_replay") is not True
        jobs = store.status()["jobs"]
        timers_verified = False
    matching = [item for item in jobs if item.get("template_id") == "crm_digest_v1"]
    if len(matching) != 1:
        raise AutomationError("automation_release_seed_readback_failed")
    if seeded and (
        matching[0].get("desired_state") != "off"
        or matching[0].get("schedule")
        != {
            "kind": "interval",
            "every_minutes": 20,
            "timezone": "Asia/Krasnoyarsk",
            "active_window": "24/7",
        }
    ):
        raise AutomationError("automation_release_seed_readback_failed")
    return {
        "ok": True,
        "seeded": seeded,
        "job_id": matching[0]["job_id"],
        "desired_state": matching[0].get("desired_state"),
        "timers_verified": timers_verified,
    }


def adopt_current(*, release_attempt_key: str) -> dict[str, Any]:
    release_attempt_key = _release_attempt_key(release_attempt_key)
    attempt_hash = _attempt_hash(release_attempt_key)
    if _socket_available(get_automation_control_socket_path()):
        raise AutomationError("automation_scheduler_already_running")
    store = AutomationStore()
    status = store.status()
    hold_state = status["global_hold"]
    if (
        hold_state["enabled"] is not True
        or hold_state["reason"] != "release"
        or hold_state.get("attempt_hash") != attempt_hash
    ):
        raise AutomationError("automation_release_hold_ownership_lost")
    controller = SystemTimerController(store=store)
    adopted = controller.adopt_all_current()
    if len(adopted) != len(SYSTEM_TIMER_ALLOWLIST) or any(item.get("error_code") is not None for item in adopted):
        raise AutomationError("automation_release_timer_adoption_failed")
    service = AutomationControlService(store, timer_controller=controller)
    readback = service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": "status",
            "actor": {"kind": "system", "id": "release-preflight", "is_admin": True},
            "payload": {},
        }
    )
    _require_timer_readback(readback)
    return {"ok": True, "timers_verified": True, "timers": adopted}


def release_hold(*, release_attempt_key: str) -> dict[str, Any]:
    release_attempt_key = _release_attempt_key(release_attempt_key)
    attempt_hash = _attempt_hash(release_attempt_key)
    socket_path = get_automation_control_socket_path()
    release_key = _command_key("release-unhold", release_attempt_key)
    if _socket_available(socket_path):
        client = _socket_client(socket_path)
        status = _client_status(client)
        hold_state = status.get("global_hold")
        if not isinstance(hold_state, dict) or type(hold_state.get("revision")) is not int:
            raise AutomationError("automation_release_hold_state_invalid")
        if (
            hold_state.get("enabled") is not True
            or hold_state.get("reason") != "release"
            or hold_state.get("attempt_hash") != attempt_hash
        ):
            raise AutomationError("automation_release_hold_ownership_lost")
        response = client.request(
            "set_global_hold",
            {"enabled": False, "reason": None, "attempt_hash": attempt_hash},
            idempotency_key=release_key,
            expected_revision=int(hold_state["revision"]),
        )
        if response.get("ok") is not True:
            raise AutomationError("automation_release_unhold_failed")
        readback = _client_status(client)
        final = readback.get("global_hold")
    else:
        store = AutomationStore()
        status = store.status()
        hold_state = status["global_hold"]
        if (
            hold_state["enabled"] is not True
            or hold_state["reason"] != "release"
            or hold_state.get("attempt_hash") != attempt_hash
        ):
            raise AutomationError("automation_release_hold_ownership_lost")
        final = _local_request(
            AutomationControlService(store),
            "set_global_hold",
            {"enabled": False, "reason": None, "attempt_hash": attempt_hash},
            idempotency_key=release_key,
            expected_revision=int(hold_state["revision"]),
        )["global_hold"]
    if not isinstance(final, dict) or final.get("enabled") is not False:
        raise AutomationError("automation_release_unhold_readback_failed")
    return {"ok": True, "global_hold": final}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("hold", "prepare-held", "seed", "adopt-current", "release-hold"),
    )
    parser.add_argument("--release-attempt-key", default="")
    args = parser.parse_args(argv)
    load_runtime_env()
    try:
        if args.operation == "adopt-current":
            result = adopt_current(release_attempt_key=args.release_attempt_key)
        elif args.operation in {"hold", "prepare-held"}:
            result = hold(release_attempt_key=args.release_attempt_key)
        elif args.operation == "seed":
            result = seed(release_attempt_key=args.release_attempt_key)
        else:
            result = release_hold(release_attempt_key=args.release_attempt_key)
    except (AutomationError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, AutomationError) else "automation_release_preflight_failed"
        print(canonical_json({"ok": False, "error": code}))
        return 1
    print(canonical_json(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
