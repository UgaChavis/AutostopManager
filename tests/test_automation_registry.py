from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import (
    AutomationError,
    AutomationStore,
    is_in_active_window,
    next_scheduled_time,
)


def request(
    service: AutomationControlService,
    operation: str,
    payload: dict | None = None,
    *,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    actor: dict | None = None,
):
    message = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": actor or {"kind": "codex", "id": "test-owner", "is_admin": True},
        "payload": payload or {},
    }
    if idempotency_key is not None:
        message["idempotency_key"] = idempotency_key
    if expected_revision is not None:
        message["expected_revision"] = expected_revision
    return service.handle(message)


def create_digest(service: AutomationControlService, key: str = "create-digest-0001") -> dict:
    return request(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1"},
        idempotency_key=key,
    )["job"]


def enable_digest(service: AutomationControlService, job: dict, key: str = "enable-digest-helper-0001") -> dict:
    request(
        service,
        "set_enabled",
        {"job_id": job["job_id"], "enabled": True},
        idempotency_key=key,
        expected_revision=job["revision"],
    )["job"]
    reconciled = service.store.reconcile_next_job()
    assert reconciled and reconciled["applied"] is True
    return reconciled["job"]


def test_schema_is_component_versioned_without_hijacking_sqlite_user_version(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")

    store = AutomationStore(path)
    store.initialize()

    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute(
            "SELECT version FROM manager_automation_schema WHERE component = 'automation_center'"
        ).fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {
        "manager_automations",
        "manager_automation_runtime",
        "manager_automation_commands",
        "manager_automation_runs",
        "manager_automation_cursors",
        "manager_automation_outbox",
        "manager_automation_schema",
    } <= tables
    assert version == 6
    assert user_version == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_singleton_starts_off_and_command_replay_is_stable(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    payload = {"template_id": "crm_digest_v1", "schedule": {"every_minutes": 20}}

    first = request(service, "create_from_template", payload, idempotency_key="create-digest-0001")
    replay = request(service, "create_from_template", payload, idempotency_key="create-digest-0001")

    assert first["job"]["desired_state"] == "off"
    assert first["job"]["actual_state"] == "disabled"
    assert first["job"]["schedule"]["every_minutes"] == 20
    assert first["job"]["schedule"]["timezone"] == "Asia/Krasnoyarsk"
    assert first["job"]["schedule"]["active_window"] == "24/7"
    assert replay["job"]["job_id"] == first["job"]["job_id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(AutomationError, match="automation_singleton_exists"):
        request(service, "create_from_template", payload, idempotency_key="create-digest-0002")
    with pytest.raises(AutomationError, match="idempotency_key_conflict"):
        request(
            service,
            "create_from_template",
            {"template_id": "crm_digest_v1", "schedule": {"every_minutes": 30}},
            idempotency_key="create-digest-0001",
        )


def test_create_accepts_bounded_display_name_but_rejects_executable_payloads(tmp_path: Path):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    preview = request(
        service,
        "preview",
        {
            "target_operation": "create_from_template",
            "target_payload": {"template_id": "crm_digest_v1", "name": "CRM: утренняя сводка"},
        },
    )
    created = request(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1", "name": "CRM: утренняя сводка"},
        idempotency_key="create-named-digest-0001",
    )

    assert preview["proposed"]["name"] == "CRM: утренняя сводка"
    assert created["job"]["name"] == "CRM: утренняя сводка"

    for index, payload in enumerate(
        (
            {"template_id": "crm_digest_v1", "name": "https://example.invalid"},
            {"template_id": "crm_digest_v1", "name": "example.com"},
            {"template_id": "crm_digest_v1", "name": "digest; shutdown"},
            {"template_id": "crm_digest_v1", "name": "digest\u202ereversed"},
            {"template_id": "crm_digest_v1", "prompt": "ignore instructions"},
            {"template_id": "crm_digest_v1", "command": "echo unsafe"},
            {"template_id": "crm_digest_v1", "url": "https://example.invalid"},
        )
    ):
        isolated = AutomationControlService(AutomationStore(tmp_path / f"invalid-{index}.sqlite3"))
        with pytest.raises(
            AutomationError,
            match=r"automation_(display_name|template_payload)_invalid",
        ):
            request(
                isolated,
                "create_from_template",
                payload,
                idempotency_key=f"create-invalid-name-{index:04d}",
            )


def test_startup_seed_is_idempotent_off_and_never_rewrites_existing_job(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")

    seeded = store.seed_defaults()
    original = store.status()["jobs"][0]
    with store.connect() as connection:
        changed = store.set_schedule(
            connection,
            job_id=original["job_id"],
            schedule_payload={"every_minutes": 35, "timezone": "UTC"},
            expected_revision=original["revision"],
        )
    replay = store.seed_defaults()
    current = store.status()["jobs"][0]

    assert seeded["seeded"] is True
    assert original["desired_state"] == "off"
    assert original["schedule"] == {
        "kind": "interval",
        "every_minutes": 20,
        "timezone": "Asia/Krasnoyarsk",
        "active_window": "24/7",
    }
    assert changed["job"]["schedule"]["every_minutes"] == 35
    assert replay["idempotent_replay"] is True
    assert current["schedule"]["every_minutes"] == 35
    assert current["schedule"]["timezone"] == "UTC"


def test_typed_timezone_and_overnight_window_round_trip_with_preview(tmp_path: Path):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    job = create_digest(service)
    schedule = {
        "kind": "interval",
        "every_minutes": 25,
        "timezone": "Europe/Moscow",
        "active_window": {"start": "22:00", "end": "06:00"},
    }

    preview = request(
        service,
        "preview",
        {
            "target_operation": "set_schedule",
            "target_payload": {"job_id": job["job_id"], "schedule": schedule},
        },
    )
    changed = request(
        service,
        "set_schedule",
        {"job_id": job["job_id"], "schedule": schedule},
        idempotency_key="schedule-digest-typed-0001",
        expected_revision=job["revision"],
    )

    assert preview["proposed"]["schedule"] == schedule
    assert changed["job"]["schedule"] == schedule
    assert service.store.status(job_id=job["job_id"])["jobs"][0]["schedule"] == schedule


@pytest.mark.parametrize(
    "schedule,error",
    [
        ({"timezone": "Mars/Olympus"}, "automation_timezone_invalid"),
        ({"active_window": {"start": "25:00", "end": "06:00"}}, "automation_active_window_invalid"),
        ({"active_window": {"start": "09:00", "end": "09:00"}}, "automation_active_window_invalid"),
        ({"kind": "cron"}, "automation_schedule_invalid"),
    ],
)
def test_typed_schedule_validation_is_fail_closed(tmp_path: Path, schedule: dict, error: str):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    job = create_digest(service)
    with pytest.raises(AutomationError, match=error):
        request(
            service,
            "set_schedule",
            {"job_id": job["job_id"], "schedule": schedule},
            idempotency_key="schedule-invalid-0001",
            expected_revision=job["revision"],
        )


def test_next_run_respects_daytime_and_overnight_windows():
    daytime = {"start": "09:00", "end": "18:00"}
    overnight = {"start": "22:00", "end": "06:00"}
    # 10:50 UTC is 17:50 in Krasnoyarsk; +20m falls after closing and is
    # coalesced to the next local opening (02:00 UTC the next day).
    next_daytime = next_scheduled_time(
        datetime(2026, 9, 21, 10, 50, tzinfo=UTC),
        delay_seconds=20 * 60,
        timezone="Asia/Krasnoyarsk",
        active_window=daytime,
    )

    assert next_daytime == datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
    assert is_in_active_window(
        datetime(2026, 9, 21, 20, 0, tzinfo=UTC),
        timezone="Asia/Krasnoyarsk",
        active_window=overnight,
    )
    assert not is_in_active_window(
        datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
        timezone="Asia/Krasnoyarsk",
        active_window=overnight,
    )


def test_missed_interval_is_coalesced_to_window_opening(monkeypatch, tmp_path: Path):
    current = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("autostop_manager.automation_registry.utc_now", lambda: current)
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = create_digest(service)
    scheduled = request(
        service,
        "set_schedule",
        {
            "job_id": job["job_id"],
            "schedule": {
                "kind": "interval",
                "every_minutes": 20,
                "timezone": "UTC",
                "active_window": {"start": "22:00", "end": "06:00"},
            },
        },
        idempotency_key="schedule-window-0001",
        expected_revision=job["revision"],
    )["job"]
    request(
        service,
        "set_enabled",
        {"job_id": job["job_id"], "enabled": True},
        idempotency_key="enable-window-0001",
        expected_revision=scheduled["revision"],
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE manager_automation_runtime SET next_run_at = ? WHERE job_id = ?",
            ("2026-09-21T11:00:00Z", job["job_id"]),
        )

    assert store.claim_next_run(owner="window-worker") is None
    assert store.status(job_id=job["job_id"])["jobs"][0]["next_run_at"] == "2026-09-21T22:00:00Z"

    current = datetime(2026, 9, 21, 22, 5, tzinfo=UTC)
    claim = store.claim_next_run(owner="window-worker")
    assert claim and claim["trigger_kind"] == "schedule"
    with sqlite3.connect(store.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM manager_automation_runs WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()[0]
    assert count == 1


def test_cas_controls_schedule_disable_and_archive(tmp_path: Path):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    job = create_digest(service)
    job_id = job["job_id"]

    enabled = request(
        service,
        "set_enabled",
        {"job_id": job_id, "enabled": True},
        idempotency_key="enable-digest-0001",
        expected_revision=1,
    )["job"]
    assert enabled["desired_state"] == "on"
    assert enabled["revision"] == 2
    assert enabled["applied_revision"] == 1
    assert enabled["reconcile_state"] == "pending"
    assert enabled["next_run_at"] is None
    reconciled = service.store.reconcile_next_job()
    assert reconciled and reconciled["applied"] is True
    assert reconciled["job"]["applied_revision"] == 2
    assert reconciled["job"]["reconcile_state"] == "in_sync"
    assert reconciled["job"]["next_run_at"]

    with pytest.raises(AutomationError, match="automation_revision_conflict"):
        request(
            service,
            "set_schedule",
            {"job_id": job_id, "schedule": {"every_minutes": 30}},
            idempotency_key="schedule-digest-0001",
            expected_revision=1,
        )
    scheduled = request(
        service,
        "set_schedule",
        {"job_id": job_id, "schedule": {"every_minutes": 30}},
        idempotency_key="schedule-digest-0002",
        expected_revision=2,
    )["job"]
    assert scheduled["revision"] == 3
    assert scheduled["schedule"]["every_minutes"] == 30
    with pytest.raises(AutomationError, match="automation_disable_before_archive"):
        request(
            service,
            "archive",
            {"job_id": job_id},
            idempotency_key="archive-digest-0001",
            expected_revision=3,
        )
    disabled = request(
        service,
        "set_enabled",
        {"job_id": job_id, "enabled": False},
        idempotency_key="disable-digest-0001",
        expected_revision=3,
    )["job"]
    archived = request(
        service,
        "archive",
        {"job_id": job_id},
        idempotency_key="archive-digest-0002",
        expected_revision=disabled["revision"],
    )
    assert archived == {"archived": True, "job_id": job_id, "revision": 5}
    assert service.store.status()["jobs"] == []


def test_off_job_rejects_run_now_and_disable_cancels_existing_queue(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    created = create_digest(service)
    with pytest.raises(AutomationError, match="automation_not_enabled"):
        request(
            service,
            "run_now",
            {"job_id": created["job_id"]},
            idempotency_key="off-run-rejected-0001",
            expected_revision=created["revision"],
        )
    enabled = enable_digest(service, created)
    queued = request(
        service,
        "run_now",
        {"job_id": enabled["job_id"]},
        idempotency_key="queued-before-disable-0001",
        expected_revision=enabled["revision"],
    )
    disabled = request(
        service,
        "set_enabled",
        {"job_id": enabled["job_id"], "enabled": False},
        idempotency_key="disable-with-queue-0001",
        expected_revision=enabled["revision"],
    )["job"]

    assert disabled["reconcile_state"] == "pending"
    assert store.claim_next_run(owner="must-not-claim") is None
    with sqlite3.connect(store.path) as connection:
        status = connection.execute(
            "SELECT status FROM manager_automation_runs WHERE run_id = ?", (queued["run_id"],)
        ).fetchone()[0]
    assert status == "cancelled"


@pytest.mark.parametrize("claim_delivery", [False, True])
def test_archive_rejects_pending_or_sending_delivery(tmp_path: Path, claim_delivery: bool):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = create_digest(service)
    request(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        idempotency_key=f"archive-delivery-{'sending' if claim_delivery else 'queued'}-0001",
        expected_revision=job["revision"],
    )
    if claim_delivery:
        assert store.claim_outbox(owner="delivery-worker")
    disabled = request(
        service,
        "set_enabled",
        {"job_id": job["job_id"], "enabled": False},
        idempotency_key=f"archive-disable-{'sending' if claim_delivery else 'queued'}-0001",
        expected_revision=job["revision"],
    )["job"]

    with pytest.raises(AutomationError, match="automation_delivery_pending"):
        request(
            service,
            "archive",
            {"job_id": job["job_id"]},
            idempotency_key=f"archive-blocked-{'sending' if claim_delivery else 'queued'}-0001",
            expected_revision=disabled["revision"],
        )


def test_run_lease_fencing_and_cursor_cas_reject_stale_worker(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))
    queued = request(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        idempotency_key="run-digest-0001",
        expected_revision=job["revision"],
    )

    claim = store.claim_next_run(owner="worker-a", lease_seconds=60)

    assert claim and claim["run_id"] == queued["run_id"]
    assert (
        store.heartbeat_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="worker-b",
            fencing_token=claim["fencing_token"],
        )
        is False
    )
    with pytest.raises(AutomationError, match="automation_fencing_conflict"):
        store.update_cursor(
            job_id=job["job_id"],
            cursor_name="crm_changes",
            cursor_value="opaque-technical-cursor",
            expected_revision=0,
            owner="worker-a",
            fencing_token=claim["fencing_token"] + 1,
        )
    cursor = store.update_cursor(
        job_id=job["job_id"],
        cursor_name="crm_changes",
        cursor_value="opaque-technical-cursor",
        expected_revision=0,
        owner="worker-a",
        fencing_token=claim["fencing_token"],
    )
    assert cursor["revision"] == 1
    assert (
        store.finish_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="worker-b",
            fencing_token=claim["fencing_token"],
            succeeded=True,
            result_code="ok",
        )
        is False
    )
    assert (
        store.finish_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="worker-a",
            fencing_token=claim["fencing_token"],
            succeeded=True,
            result_code="ok",
        )
        is True
    )


def test_notification_test_persists_only_technical_intent(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    service = AutomationControlService(AutomationStore(path))
    job = create_digest(service)

    result = request(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        idempotency_key="notify-test-0001",
        expected_revision=job["revision"],
        actor={"kind": "telegram_owner", "id": "private-handle-not-stored", "is_admin": False},
    )

    assert result["external_message_sent"] is False
    with sqlite3.connect(path) as connection:
        command = connection.execute("SELECT actor_hash, response_json FROM manager_automation_commands").fetchone()
        outbox = connection.execute("SELECT payload_json, status FROM manager_automation_outbox").fetchone()
    assert len(command[0]) == 64
    assert "private-handle-not-stored" not in command[1]
    assert "private-handle-not-stored" not in outbox[0]
    assert outbox[1] == "queued"

    claim = service.store.claim_outbox(owner="delivery-worker-a", lease_seconds=60)
    assert claim and claim["outbox_id"] == result["outbox_id"]
    assert (
        service.store.finish_outbox(
            outbox_id=claim["outbox_id"],
            owner="delivery-worker-b",
            fencing_token=claim["fencing_token"],
            sent=True,
            result_code="ok",
        )
        is False
    )
    assert (
        service.store.finish_outbox(
            outbox_id=claim["outbox_id"],
            owner="delivery-worker-a",
            fencing_token=claim["fencing_token"],
            sent=True,
            result_code="ok",
        )
        is True
    )


def test_non_admin_crm_actor_cannot_mutate(tmp_path: Path):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    with pytest.raises(AutomationError, match="automation_permission_denied"):
        request(
            service,
            "create_from_template",
            {"template_id": "crm_digest_v1"},
            idempotency_key="create-digest-0001",
            actor={"kind": "crm_operator", "id": "operator", "is_admin": False},
        )


def test_global_hold_is_cas_guarded_and_blocks_scheduler_claims(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))
    request(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        idempotency_key="run-digest-0001",
        expected_revision=job["revision"],
    )

    held = request(
        service,
        "set_global_hold",
        {"enabled": True, "reason": "release", "attempt_hash": "d" * 64},
        idempotency_key="global-hold-0001",
        expected_revision=0,
    )

    assert held["global_hold"] == {
        "enabled": True,
        "reason": "release",
        "attempt_hash": "d" * 64,
        "revision": 1,
    }
    assert store.claim_next_run(owner="worker-a") is None
    with pytest.raises(AutomationError, match="automation_revision_conflict"):
        request(
            service,
            "set_global_hold",
            {"enabled": False, "reason": None, "attempt_hash": "d" * 64},
            idempotency_key="global-hold-0002",
            expected_revision=0,
        )
    released = request(
        service,
        "set_global_hold",
        {"enabled": False, "reason": None, "attempt_hash": "d" * 64},
        idempotency_key="global-hold-0003",
        expected_revision=1,
    )
    assert released["global_hold"]["revision"] == 2
    assert store.claim_next_run(owner="worker-a") is not None


def test_error_incident_alert_is_suppressed_until_one_recovery(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))

    def finish(key: str, *, succeeded: bool, code: str) -> None:
        request(
            service,
            "run_now",
            {"job_id": job["job_id"]},
            idempotency_key=key,
            expected_revision=job["revision"],
        )
        claim = store.claim_next_run(owner="incident-worker")
        assert claim
        assert store.finish_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="incident-worker",
            fencing_token=claim["fencing_token"],
            succeeded=succeeded,
            result_code=code,
        )

    finish("incident-run-0001", succeeded=False, code="automation_crm_transport_failed")
    finish("incident-run-0002", succeeded=False, code="automation_crm_transport_failed")
    finish("incident-run-0003", succeeded=True, code="ok")
    finish("incident-run-0004", succeeded=True, code="ok")

    with sqlite3.connect(path) as connection:
        alerts = connection.execute(
            "SELECT kind, payload_json FROM manager_automation_outbox ORDER BY created_at, outbox_id"
        ).fetchall()
        incident = connection.execute(
            "SELECT incident_id, incident_code FROM manager_automation_runtime WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
    assert sorted(row[0] for row in alerts) == ["job_error_alert", "job_recovery_alert"]
    error_payload = next(row[1] for row in alerts if row[0] == "job_error_alert")
    assert "automation_crm_transport_failed" in error_payload
    assert incident == (None, None)


def test_expired_lease_opens_one_incident_atomically(monkeypatch, tmp_path: Path):
    current = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("autostop_manager.automation_registry.utc_now", lambda: current)
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))
    request(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        idempotency_key="lease-incident-run-0001",
        expected_revision=job["revision"],
    )
    assert store.claim_next_run(owner="expired-worker", lease_seconds=10)

    current = datetime(2026, 9, 21, 12, 0, 11, tzinfo=UTC)
    assert store.claim_next_run(owner="replacement-worker") is None

    with sqlite3.connect(path) as connection:
        runtime = connection.execute(
            "SELECT error_code, incident_id FROM manager_automation_runtime WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
        alerts = connection.execute(
            "SELECT COUNT(*) FROM manager_automation_outbox WHERE kind = 'job_error_alert'"
        ).fetchone()[0]
    assert runtime[0] == "automation_lease_expired"
    assert runtime[1].startswith("inc_")
    assert alerts == 1
