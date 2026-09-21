from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autostop_manager import automation_release
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_timers import SYSTEM_TIMER_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def _timer_readback() -> list[dict[str, Any]]:
    return [
        {
            "timer_id": timer_id,
            "revision": 1,
            "error_code": None,
            "reconcile_state": "in_sync",
        }
        for timer_id in SYSTEM_TIMER_ALLOWLIST
    ]


def _socket_status(
    *,
    attempt: str,
    enabled: bool = True,
    reason: str | None = "release",
    revision: int = 1,
    jobs: list[dict[str, Any]] | None = None,
    sending: int = 0,
    lease_until: str | None = None,
) -> dict[str, Any]:
    return {
        "global_hold": {
            "enabled": enabled,
            "reason": reason,
            "attempt_hash": automation_release._attempt_hash(attempt) if enabled else None,
            "revision": revision,
        },
        "jobs": [
            *([] if jobs is None else jobs),
            *([] if lease_until is None else [{"lease_until": lease_until}]),
        ],
        "system_timers": _timer_readback(),
        "readiness": {
            "execution_packet": {
                "outbox": {"by_status": {"sending": sending}},
            }
        },
    }


class _LifecycleClient:
    def __init__(
        self,
        *,
        attempt: str,
        initial_hold: bool = False,
        busy_readbacks: int = 0,
        fail_operation: str | None = None,
    ):
        self.attempt = attempt
        self.hold = initial_hold
        self.revision = 1 if initial_hold else 0
        self.busy_readbacks = busy_readbacks
        self.fail_operation = fail_operation
        self.jobs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def request(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation, payload, kwargs))
        if operation == "status":
            busy = self.busy_readbacks > 0
            if busy:
                self.busy_readbacks -= 1
            data = _socket_status(
                attempt=self.attempt,
                enabled=self.hold,
                reason="release" if self.hold else None,
                revision=self.revision,
                jobs=self.jobs,
                sending=int(busy),
            )
            return {"ok": True, "data": data}
        if operation == self.fail_operation:
            return {"ok": False, "error": "forced"}
        if operation == "set_global_hold":
            self.hold = bool(payload["enabled"])
            self.revision += 1
            return {"ok": True}
        if operation == "create_from_template":
            self.jobs.append(
                {
                    "job_id": "crm-digest",
                    "template_id": "crm_digest_v1",
                    "desired_state": "off",
                    "schedule": {
                        "kind": "interval",
                        "every_minutes": 20,
                        "timezone": "Asia/Krasnoyarsk",
                        "active_window": "24/7",
                    },
                }
            )
            return {"ok": True}
        raise AssertionError(operation)


def test_release_preflight_holds_before_idempotent_off_seed(tmp_path: Path):
    db = tmp_path / "registry.sqlite3"
    socket_path = tmp_path / "missing.sock"
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AUTOSTOP_MANAGER_ENV_FILE": "/dev/null",
        "AUTOSTOP_AUTOMATION_DB": str(db),
        "AUTOSTOP_AUTOMATION_CONTROL_SOCKET": str(socket_path),
    }
    hold_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "hold",
        "--release-attempt-key",
        "release-attempt-test-1",
    ]
    seed_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "seed",
        "--release-attempt-key",
        "release-attempt-test-1",
    ]
    wrong_unhold_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "release-hold",
        "--release-attempt-key",
        "release-attempt-test-2",
    ]

    first = subprocess.run(hold_command, env=environment, capture_output=True, text=True, timeout=10, check=False)
    wrong_unhold = subprocess.run(
        wrong_unhold_command, env=environment, capture_output=True, text=True, timeout=10, check=False
    )
    seeded = subprocess.run(seed_command, env=environment, capture_output=True, text=True, timeout=10, check=False)
    second = subprocess.run(seed_command, env=environment, capture_output=True, text=True, timeout=10, check=False)

    assert first.returncode == 0, first.stdout + first.stderr
    assert wrong_unhold.returncode == 1
    assert json.loads(wrong_unhold.stdout)["error"] == "automation_release_hold_ownership_lost"
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    first_hold = json.loads(first.stdout)["global_hold"]
    assert first_hold["enabled"] is True
    assert first_hold["reason"] == "release"
    assert first_hold["revision"] == 1
    assert len(first_hold["attempt_hash"]) == 64
    assert json.loads(seeded.stdout)["seeded"] is True
    assert json.loads(second.stdout)["seeded"] is False
    status_code = """
from autostop_manager.automation_registry import AutomationStore
import json
print(json.dumps(AutomationStore().status()))
"""
    status_result = subprocess.run(
        [str(PYTHON), "-c", status_code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    status = json.loads(status_result.stdout)
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["desired_state"] == "off"
    assert status["jobs"][0]["schedule"] == {
        "kind": "interval",
        "every_minutes": 20,
        "timezone": "Asia/Krasnoyarsk",
        "active_window": "24/7",
    }


def test_installer_prepares_hold_before_service_activation():
    source = (ROOT / "scripts/install-manager-automation.sh").read_text(encoding="utf-8")

    assert "--activate-under-hold" in source
    assert "git -C" not in source
    assert "--release-attempt-key" in source
    assert source.index("automation_release hold") < source.index('install -o root -g root -m 0644 "${UNIT_SOURCE}"')
    assert source.index("automation_release hold") < source.index('systemctl enable --now "${UNIT_NAME}"')
    assert source.index("automation_release adopt-current") < source.index('systemctl enable --now "${UNIT_NAME}"')
    assert source.index("automation_release seed") > source.index('systemctl enable --now "${UNIT_NAME}"')


def test_offline_timer_adoption_requires_owned_hold_and_exact_readback(monkeypatch, tmp_path: Path):
    database = tmp_path / "registry.sqlite3"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(database))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "missing.sock"))
    attempt = "release-adopt-test-0001"
    automation_release.hold(release_attempt_key=attempt)

    class FakeTimerController:
        def __init__(self, *, store):
            self.store = store

        @staticmethod
        def _state():
            return {
                "load_state": "loaded",
                "active_state": "active",
                "unit_file_state": "enabled",
                "desired_state": "on",
                "actual_state": "active",
                "period_minutes": 20,
                "next_run_at": None,
                "last_run_at": None,
                "error_code": None,
            }

        def adopt_all_current(self):
            return [
                self.store.adopt_system_timer(
                    timer_id=timer_id,
                    unit_name=policy.unit_name,
                    control_mode=policy.control_mode,
                    state=self._state(),
                )
                for timer_id, policy in sorted(SYSTEM_TIMER_ALLOWLIST.items())
            ]

        def list_status(self):
            return [
                {
                    "timer_id": timer_id,
                    "name": policy.display_name,
                    "unit_name": policy.unit_name,
                    "control_mode": policy.control_mode,
                    "locked": policy.control_mode == "read_only",
                    "inspection_ok": True,
                    "state": self._state(),
                }
                for timer_id, policy in sorted(SYSTEM_TIMER_ALLOWLIST.items())
            ]

    monkeypatch.setattr(automation_release, "SystemTimerController", FakeTimerController)

    result = automation_release.adopt_current(release_attempt_key=attempt)

    assert result["timers_verified"] is True
    assert len(result["timers"]) == len(SYSTEM_TIMER_ALLOWLIST)
    assert len(AutomationStore(database).status()["system_timers"]) == len(SYSTEM_TIMER_ALLOWLIST)
    with pytest.raises(AutomationError, match="automation_release_hold_ownership_lost"):
        automation_release.adopt_current(release_attempt_key="release-adopt-test-0002")


def test_release_keys_local_request_and_socket_path_guards(tmp_path: Path):
    release_key = "release-attempt-valid-0001"
    assert automation_release._command_key("seed", release_key).startswith("seed:")
    assert len(automation_release._attempt_hash(release_key)) == 64
    assert automation_release._release_attempt_key(release_key) == release_key
    for invalid in ("", "short", " has-space", "bad/character", "x" * 129):
        with pytest.raises(AutomationError, match="automation_release_attempt_key_invalid"):
            automation_release._release_attempt_key(invalid)

    captured: list[dict[str, Any]] = []

    class CapturingService:
        def handle(self, request: dict[str, Any]) -> dict[str, Any]:
            captured.append(request)
            return {"ok": True}

    service = CapturingService()
    assert automation_release._local_request(
        service,  # type: ignore[arg-type]
        "set_global_hold",
        {"enabled": True},
        idempotency_key="hold-key",
        expected_revision=7,
    ) == {"ok": True}
    assert captured[0]["protocol"] == automation_release.AUTOMATION_CONTROL_PROTOCOL
    assert captured[0]["expected_revision"] == 7
    assert captured[0]["actor"]["kind"] == "system"
    automation_release._local_request(
        service,  # type: ignore[arg-type]
        "status",
        {},
        idempotency_key="status-key",
    )
    assert "expected_revision" not in captured[1]

    missing = tmp_path / "missing.sock"
    assert automation_release._socket_available(missing) is False
    regular = tmp_path / "regular.sock"
    regular.write_text("not a socket", encoding="utf-8")
    with pytest.raises(AutomationError, match="automation_socket_path_unsafe"):
        automation_release._socket_available(regular)
    actual = tmp_path / "actual.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(actual))
        assert automation_release._socket_available(actual) is True
    finally:
        listener.close()


def test_socket_client_uses_release_system_actor(monkeypatch, tmp_path: Path):
    captured: dict[str, Any] = {}

    class FakeControlClient:
        def __init__(self, **kwargs: Any):
            captured.update(kwargs)

    monkeypatch.setattr(automation_release, "AutomationControlClient", FakeControlClient)
    path = tmp_path / "control.sock"
    client = automation_release._socket_client(path)
    assert isinstance(client, FakeControlClient)
    assert captured == {
        "socket_path": path,
        "actor": {"kind": "system", "id": "release-preflight", "is_admin": True},
    }


def test_client_status_contract_and_quiescence_guards(monkeypatch):
    attempt = "release-status-test-0001"
    ready = _socket_status(attempt=attempt)

    class StaticClient:
        def __init__(self, response: dict[str, Any]):
            self.response = response

        def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert operation == "status"
            assert payload == {"include_archived": True}
            return self.response

    assert automation_release._client_status(StaticClient({"ok": True, "data": ready})) == ready  # type: ignore[arg-type]
    for response in ({"ok": False, "data": ready}, {"ok": True, "data": []}, {}):
        with pytest.raises(AutomationError, match="automation_release_status_failed"):
            automation_release._client_status(StaticClient(response))  # type: ignore[arg-type]

    automation_release._require_quiescent(ready)
    future = (automation_release.utc_now() + timedelta(minutes=1)).isoformat()
    invalid_statuses = [
        {**ready, "jobs": ["not-an-object"]},
        {**ready, "jobs": [{"lease_until": future}]},
        {**ready, "readiness": None},
        {**ready, "readiness": {"execution_packet": {"outbox": {}}}},
        _socket_status(attempt=attempt, sending=1),
    ]
    expected_errors = [
        "automation_release_status_invalid",
        "automation_release_not_quiescent",
        "automation_release_quiescence_unavailable",
        "automation_release_quiescence_unavailable",
        "automation_release_not_quiescent",
    ]
    for status, error in zip(invalid_statuses, expected_errors, strict=True):
        with pytest.raises(AutomationError, match=error):
            automation_release._require_quiescent(status)

    responses = [
        {"ok": True, "data": _socket_status(attempt=attempt, sending=1)},
        {"ok": True, "data": ready},
    ]

    class SequenceClient:
        def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            return responses.pop(0)

    monkeypatch.setattr(automation_release.time, "sleep", lambda _seconds: None)
    assert (
        automation_release._wait_quiescent(
            SequenceClient(),  # type: ignore[arg-type]
            _socket_status(attempt=attempt, sending=1),
        )
        == ready
    )
    with pytest.raises(AutomationError, match="automation_release_quiescence_unavailable"):
        automation_release._wait_quiescent(SequenceClient(), {**ready, "readiness": None})  # type: ignore[arg-type]


def test_wait_quiescent_stops_after_bounded_busy_retries(monkeypatch):
    attempt = "release-busy-test-0001"
    busy = _socket_status(attempt=attempt, sending=1)
    calls = 0

    class BusyClient:
        def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"ok": True, "data": busy}

    monkeypatch.setattr(automation_release.time, "sleep", lambda _seconds: None)
    with pytest.raises(AutomationError, match="automation_release_not_quiescent"):
        automation_release._wait_quiescent(BusyClient(), busy)  # type: ignore[arg-type]
    assert calls == 49


def test_timer_readback_requires_exact_allowlist_and_healthy_revisions():
    valid = {"system_timers": _timer_readback()}
    automation_release._require_timer_readback(valid)
    automation_release._require_timer_readback(
        {"system_timers": [*_timer_readback(), {"timer_id": 123}, "ignored-noise"]}
    )

    def changed(**updates: Any) -> dict[str, Any]:
        timers = _timer_readback()
        timers[0] = {**timers[0], **updates}
        return {"system_timers": timers}

    invalid = [
        {},
        {"system_timers": {}},
        {"system_timers": _timer_readback()[1:]},
        changed(revision=True),
        changed(revision=0),
        changed(error_code="system_timer_failed"),
        changed(reconcile_state="drift"),
    ]
    for status in invalid:
        with pytest.raises(AutomationError, match="automation_release_timer_readback_failed"):
            automation_release._require_timer_readback(status)


def test_socket_release_lifecycle_waits_seeds_once_and_unholds(monkeypatch, tmp_path: Path):
    attempt = "release-socket-test-0001"
    client = _LifecycleClient(attempt=attempt, busy_readbacks=2)
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: True)
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: client)
    monkeypatch.setattr(automation_release.time, "sleep", lambda _seconds: None)

    held = automation_release.hold(release_attempt_key=attempt)
    assert held["mode"] == "socket"
    assert held["quiescent"] is True
    assert client.hold is True
    assert automation_release.hold(release_attempt_key=attempt)["global_hold"]["revision"] == 1

    first_seed = automation_release.seed(release_attempt_key=attempt)
    second_seed = automation_release.seed(release_attempt_key=attempt)
    assert first_seed == {
        "ok": True,
        "seeded": True,
        "job_id": "crm-digest",
        "desired_state": "off",
        "timers_verified": True,
    }
    assert second_seed["seeded"] is False
    assert len([call for call in client.calls if call[0] == "create_from_template"]) == 1

    released = automation_release.release_hold(release_attempt_key=attempt)
    assert released["global_hold"]["enabled"] is False
    hold_call = next(call for call in client.calls if call[0] == "set_global_hold" and call[1]["enabled"])
    assert hold_call[2]["expected_revision"] == 0
    assert hold_call[2]["idempotency_key"].startswith("release-hold:")


def test_socket_hold_rejects_preexisting_or_unverifiable_state(monkeypatch, tmp_path: Path):
    attempt = "release-guard-test-0001"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: True)

    class StaticStatusClient:
        def __init__(self, statuses: list[dict[str, Any]], write_response: dict[str, Any] | None = None):
            self.statuses = statuses
            self.write_response = write_response or {"ok": True}

        def request(self, operation: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            if operation == "status":
                return {"ok": True, "data": self.statuses.pop(0)}
            return self.write_response

    maintenance = _socket_status(attempt=attempt, reason="maintenance")
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: StaticStatusClient([maintenance]))
    with pytest.raises(AutomationError, match="automation_release_preexisting_hold"):
        automation_release.hold(release_attempt_key=attempt)

    malformed = {**_socket_status(attempt=attempt), "global_hold": {"enabled": False, "revision": "0"}}
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: StaticStatusClient([malformed]))
    with pytest.raises(AutomationError, match="automation_release_hold_state_invalid"):
        automation_release.hold(release_attempt_key=attempt)

    off = _socket_status(attempt=attempt, enabled=False, reason=None, revision=0)
    failed = StaticStatusClient([off], {"ok": False})
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: failed)
    with pytest.raises(AutomationError, match="automation_release_hold_failed"):
        automation_release.hold(release_attempt_key=attempt)

    wrong_readback = _socket_status(attempt="release-other-test-0001")
    client = StaticStatusClient([off, wrong_readback])
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: client)
    with pytest.raises(AutomationError, match="automation_release_hold_readback_failed"):
        automation_release.hold(release_attempt_key=attempt)


def test_socket_seed_and_release_fail_closed_on_bad_ack_or_readback(monkeypatch, tmp_path: Path):
    attempt = "release-failure-test-0001"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: True)

    not_held = _LifecycleClient(attempt=attempt)
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: not_held)
    with pytest.raises(AutomationError, match="automation_release_hold_required"):
        automation_release.seed(release_attempt_key=attempt)

    create_failed = _LifecycleClient(attempt=attempt, initial_hold=True, fail_operation="create_from_template")
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: create_failed)
    with pytest.raises(AutomationError, match="automation_release_seed_failed"):
        automation_release.seed(release_attempt_key=attempt)

    unhold_failed = _LifecycleClient(attempt=attempt, initial_hold=True, fail_operation="set_global_hold")
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: unhold_failed)
    with pytest.raises(AutomationError, match="automation_release_unhold_failed"):
        automation_release.release_hold(release_attempt_key=attempt)

    wrong_owner = _LifecycleClient(attempt="release-other-test-0001", initial_hold=True)
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: wrong_owner)
    with pytest.raises(AutomationError, match="automation_release_hold_ownership_lost"):
        automation_release.release_hold(release_attempt_key=attempt)

    malformed_release = _socket_status(attempt=attempt)
    malformed_release["global_hold"]["revision"] = "1"

    class StatusOnlyClient:
        def __init__(self, statuses: list[dict[str, Any]]):
            self.statuses = statuses

        def request(self, operation: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            if operation == "status":
                return {"ok": True, "data": self.statuses.pop(0)}
            return {"ok": True}

    monkeypatch.setattr(
        automation_release,
        "_socket_client",
        lambda _path: StatusOnlyClient([malformed_release]),
    )
    with pytest.raises(AutomationError, match="automation_release_hold_state_invalid"):
        automation_release.release_hold(release_attempt_key=attempt)

    still_held = _socket_status(attempt=attempt)
    monkeypatch.setattr(
        automation_release,
        "_socket_client",
        lambda _path: StatusOnlyClient([still_held, still_held]),
    )
    with pytest.raises(AutomationError, match="automation_release_unhold_readback_failed"):
        automation_release.release_hold(release_attempt_key=attempt)


def test_socket_seed_rejects_missing_or_mutated_digest_readback(monkeypatch, tmp_path: Path):
    attempt = "release-seed-readback-0001"
    held = _socket_status(attempt=attempt)
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: True)

    class ScriptedClient:
        def __init__(self, readback: dict[str, Any]):
            self.statuses = [held, readback]

        def request(self, operation: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            if operation == "status":
                return {"ok": True, "data": self.statuses.pop(0)}
            assert operation == "create_from_template"
            return {"ok": True}

    missing = _socket_status(attempt=attempt)
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: ScriptedClient(missing))
    with pytest.raises(AutomationError, match="automation_release_seed_readback_failed"):
        automation_release.seed(release_attempt_key=attempt)

    mutated_job = {
        "job_id": "crm-digest",
        "template_id": "crm_digest_v1",
        "desired_state": "on",
        "schedule": {"kind": "interval", "every_minutes": 5},
    }
    mutated = _socket_status(attempt=attempt, jobs=[mutated_job])
    monkeypatch.setattr(automation_release, "_socket_client", lambda _path: ScriptedClient(mutated))
    with pytest.raises(AutomationError, match="automation_release_seed_readback_failed"):
        automation_release.seed(release_attempt_key=attempt)


def test_offline_hold_seed_and_release_are_owned_and_idempotent(monkeypatch, tmp_path: Path):
    attempt = "release-offline-test-0001"
    database = tmp_path / "registry.sqlite3"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(database))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "missing.sock"))

    held = automation_release.hold(release_attempt_key=attempt)
    assert held["mode"] == "offline"
    assert automation_release.hold(release_attempt_key=attempt)["global_hold"]["revision"] == 1
    assert automation_release.seed(release_attempt_key=attempt)["seeded"] is True
    assert automation_release.seed(release_attempt_key=attempt)["seeded"] is False
    released = automation_release.release_hold(release_attempt_key=attempt)
    assert released["global_hold"]["enabled"] is False
    with pytest.raises(AutomationError, match="automation_release_hold_required"):
        automation_release.seed(release_attempt_key=attempt)
    with pytest.raises(AutomationError, match="automation_release_hold_ownership_lost"):
        automation_release.release_hold(release_attempt_key=attempt)


def test_offline_hold_fail_closed_on_existing_hold_readback_and_activity(monkeypatch, tmp_path: Path):
    attempt = "release-offline-guard-0001"
    attempt_hash = automation_release._attempt_hash(attempt)
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "missing.sock"))

    class FakeStore:
        def __init__(
            self,
            statuses: list[dict[str, Any]],
            *,
            sending: int = 0,
        ):
            self.path = tmp_path / "not-created.sqlite3"
            self.statuses = statuses
            self.sending = sending

        def initialize(self) -> None:
            pass

        def status(self) -> dict[str, Any]:
            return self.statuses.pop(0)

        def technical_execution_state(self) -> dict[str, Any]:
            return {"outbox": {"by_status": {"sending": self.sending}}}

    good_hold = {
        "global_hold": {
            "enabled": True,
            "reason": "release",
            "attempt_hash": attempt_hash,
            "revision": 1,
        },
        "jobs": [],
    }
    maintenance = {
        **good_hold,
        "global_hold": {**good_hold["global_hold"], "reason": "maintenance"},
    }
    monkeypatch.setattr(automation_release, "AutomationStore", lambda: FakeStore([maintenance]))
    with pytest.raises(AutomationError, match="automation_release_preexisting_hold"):
        automation_release.hold(release_attempt_key=attempt)

    wrong_readback = {
        **good_hold,
        "global_hold": {**good_hold["global_hold"], "attempt_hash": "0" * 64},
    }
    monkeypatch.setattr(
        automation_release,
        "AutomationStore",
        lambda: FakeStore([good_hold, wrong_readback]),
    )
    with pytest.raises(AutomationError, match="automation_release_hold_readback_failed"):
        automation_release.hold(release_attempt_key=attempt)

    monkeypatch.setattr(
        automation_release,
        "AutomationStore",
        lambda: FakeStore([good_hold, good_hold], sending=1),
    )
    with pytest.raises(AutomationError, match="automation_release_not_quiescent"):
        automation_release.hold(release_attempt_key=attempt)

    leased = {
        **good_hold,
        "jobs": [{"lease_until": (automation_release.utc_now() + timedelta(minutes=1)).isoformat()}],
    }
    monkeypatch.setattr(
        automation_release,
        "AutomationStore",
        lambda: FakeStore([leased, leased]),
    )
    with pytest.raises(AutomationError, match="automation_release_not_quiescent"):
        automation_release.hold(release_attempt_key=attempt)


def test_offline_hold_requires_backup_before_schema_migration(monkeypatch, tmp_path: Path):
    database = tmp_path / "registry.sqlite3"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(database))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "missing.sock"))
    store = AutomationStore(database)
    store.initialize()
    with store.connect() as connection:
        connection.execute(
            "UPDATE manager_automation_schema SET version = ? WHERE component = 'automation_center'",
            (automation_release.AUTOMATION_SCHEMA_VERSION - 1,),
        )
    with pytest.raises(AutomationError, match="automation_release_backup_required_before_migration"):
        automation_release.hold(release_attempt_key="release-schema-test-0001")


def test_adopt_current_rejects_running_scheduler_and_incomplete_adoption(monkeypatch, tmp_path: Path):
    attempt = "release-adopt-guard-0001"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(tmp_path / "registry.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: True)
    with pytest.raises(AutomationError, match="automation_scheduler_already_running"):
        automation_release.adopt_current(release_attempt_key=attempt)

    monkeypatch.setattr(automation_release, "_socket_available", lambda _path: False)
    automation_release.hold(release_attempt_key=attempt)

    class IncompleteController:
        def __init__(self, *, store: AutomationStore):
            self.store = store

        def adopt_all_current(self) -> list[dict[str, Any]]:
            return [{"error_code": None}]

    monkeypatch.setattr(automation_release, "SystemTimerController", IncompleteController)
    with pytest.raises(AutomationError, match="automation_release_timer_adoption_failed"):
        automation_release.adopt_current(release_attempt_key=attempt)

    class FailedController(IncompleteController):
        def adopt_all_current(self) -> list[dict[str, Any]]:
            adopted = [{"error_code": None} for _timer_id in SYSTEM_TIMER_ALLOWLIST]
            adopted[0]["error_code"] = "system_timer_failed"
            return adopted

    monkeypatch.setattr(automation_release, "SystemTimerController", FailedController)
    with pytest.raises(AutomationError, match="automation_release_timer_adoption_failed"):
        automation_release.adopt_current(release_attempt_key=attempt)


@pytest.mark.parametrize(
    ("operation", "target"),
    [
        ("hold", "hold"),
        ("prepare-held", "hold"),
        ("seed", "seed"),
        ("adopt-current", "adopt_current"),
        ("release-hold", "release_hold"),
    ],
)
def test_main_dispatches_each_release_operation(monkeypatch, capsys, operation: str, target: str):
    loaded: list[bool] = []
    called: list[str] = []
    monkeypatch.setattr(automation_release, "load_runtime_env", lambda: loaded.append(True))

    def successful(*, release_attempt_key: str) -> dict[str, Any]:
        called.append(release_attempt_key)
        return {"ok": True, "operation": target}

    monkeypatch.setattr(automation_release, target, successful)
    assert automation_release.main([operation, "--release-attempt-key", "release-main-test-0001"]) == 0
    assert loaded == [True]
    assert called == ["release-main-test-0001"]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "operation": target}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AutomationError("specific_release_error"), "specific_release_error"),
        (OSError("disk unavailable"), "automation_release_preflight_failed"),
        (ValueError("invalid configuration"), "automation_release_preflight_failed"),
        (json.JSONDecodeError("invalid", "{", 0), "automation_release_preflight_failed"),
    ],
)
def test_main_serializes_expected_failures(monkeypatch, capsys, error: Exception, expected: str):
    monkeypatch.setattr(automation_release, "load_runtime_env", lambda: None)

    def failed(*, release_attempt_key: str) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(automation_release, "hold", failed)
    assert automation_release.main(["hold", "--release-attempt-key", "release-main-test-0001"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": expected}


def test_main_returns_failure_for_non_ok_result(monkeypatch, capsys):
    monkeypatch.setattr(automation_release, "load_runtime_env", lambda: None)
    monkeypatch.setattr(automation_release, "seed", lambda **_kwargs: {"ok": False, "reason": "not-ready"})
    assert automation_release.main(["seed", "--release-attempt-key", "release-main-test-0001"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "reason": "not-ready"}
