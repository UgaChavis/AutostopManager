from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import (
    AutomationError,
    AutomationStore,
    is_in_active_window,
    next_active_time,
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


@pytest.mark.parametrize("include_archived", [False, True])
def test_status_reads_bounded_ordered_jobs_in_one_query(tmp_path: Path, monkeypatch, include_archived):
    current = datetime(2026, 10, 21, tzinfo=UTC)
    monkeypatch.setattr(automation_registry, "utc_now", lambda: current)
    store = AutomationStore(tmp_path / "registry.sqlite3")
    store.initialize()
    expected = []
    with store.connect() as connection:
        for index in range(103):
            job_id = f"auto_{index:024x}"
            created = automation_registry.isoformat(current + timedelta(seconds=index // 2))
            archived = created if index % 11 == 0 else None
            connection.execute(
                """INSERT INTO manager_automations(
                    job_id,template_id,name,desired_state,schedule_kind,every_seconds,timezone,
                    revision,created_actor_hash,created_at,updated_at,archived_at)
                    VALUES(?,'crm_digest_v1','Synthetic','off','interval',1200,'UTC',1,'synthetic',?,?,?)""",
                (job_id, created, created, archived),
            )
            connection.execute(
                "INSERT INTO manager_automation_runtime(job_id,actual_state,updated_at) VALUES(?,'disabled',?)",
                (job_id, created),
            )
            if include_archived or archived is None:
                expected.append(job_id)
    statements = []
    connect = AutomationStore.connect

    @contextmanager
    def traced_connect(self):
        with connect(self) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(AutomationStore, "connect", traced_connect)

    result = store.status(include_archived=include_archived)

    assert [job["job_id"] for job in result["jobs"]] == expected[: automation_registry.MAX_JOBS]
    assert len([statement for statement in statements if "FROM manager_automations" in statement]) == 1
    assert "BEGIN IMMEDIATE" in statements  # Schema validation/migrations remain active.


@pytest.mark.parametrize("exact", [False, True])
def test_status_keeps_missing_runtime_as_an_error(tmp_path: Path, exact):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    job = create_digest(AutomationControlService(store))
    with store.connect() as connection:
        connection.execute("DELETE FROM manager_automation_runtime WHERE job_id = ?", (job["job_id"],))

    with pytest.raises(AutomationError, match=r"^automation_job_not_found$"):
        store.status(**({"job_id": job["job_id"]} if exact else {}))


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


@pytest.mark.parametrize(
    "timezone,at,window,expected",
    [
        ("Europe/Berlin", "2026-03-29T00:45:00Z", {"start": "02:30", "end": "03:15"}, "2026-03-29T01:00:00Z"),
        ("Europe/Berlin", "2026-03-29T00:45:00Z", {"start": "02:10", "end": "02:20"}, "2026-03-30T00:10:00Z"),
        ("Europe/Berlin", "2026-03-28T20:00:00Z", {"start": "02:10", "end": "02:20"}, "2026-03-30T00:10:00Z"),
        ("Europe/Berlin", "2026-10-25T00:45:00Z", {"start": "02:00", "end": "02:30"}, "2026-10-25T01:00:00Z"),
        ("Europe/Berlin", "2026-10-25T00:25:00Z", {"start": "02:10", "end": "02:20"}, "2026-10-25T01:10:00Z"),
        ("Europe/Berlin", "2026-10-25T00:45:00Z", {"start": "01:30", "end": "02:15"}, "2026-10-25T01:00:00Z"),
        ("Europe/Berlin", "2026-10-25T00:45:00Z", {"start": "22:00", "end": "02:15"}, "2026-10-25T01:00:00Z"),
        ("Australia/Lord_Howe", "2026-10-03T15:15:00Z", {"start": "02:15", "end": "02:45"}, "2026-10-03T15:30:00Z"),
        ("Australia/Lord_Howe", "2026-10-03T08:00:00Z", {"start": "02:05", "end": "02:15"}, "2026-10-04T15:05:00Z"),
        ("Pacific/Apia", "2011-12-29T20:00:00Z", {"start": "09:00", "end": "10:00"}, "2011-12-30T19:00:00Z"),
    ],
)
def test_next_active_time_uses_first_real_window_across_clock_changes(timezone, at, window, expected):
    now = datetime.fromisoformat(at)
    result = next_active_time(now, timezone=timezone, active_window=window)

    assert result == datetime.fromisoformat(expected)
    assert is_in_active_window(result, timezone=timezone, active_window=window)
    assert not is_in_active_window(result - timedelta(seconds=1), timezone=timezone, active_window=window)


@pytest.mark.parametrize("operation", ["run_now", "test_notification"])
@pytest.mark.parametrize("revision_kind", ["missing", "stale"])
def test_effect_commands_require_current_revision_without_creating_intent(tmp_path, operation, revision_kind):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))
    revision = None if revision_kind == "missing" else job["revision"] - 1
    code = "expected_revision_required" if revision_kind == "missing" else "automation_revision_conflict"

    with pytest.raises(AutomationError, match=code):
        request(
            service,
            operation,
            {"job_id": job["job_id"]},
            idempotency_key=f"guard-{operation}-{revision_kind}-0001",
            expected_revision=revision,
        )

    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM manager_automation_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM manager_automation_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["operator", "maintenance"])
@pytest.mark.parametrize("actor_id", ["release-owner", "other-operator"])
def test_release_hold_cannot_be_reclassified_without_releasing_owned_attempt(tmp_path, reason, actor_id):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    held = request(
        service,
        "set_global_hold",
        {"enabled": True, "reason": "release", "attempt_hash": "a" * 64},
        idempotency_key="owned-release-hold-0001",
        expected_revision=0,
        actor={"kind": "system", "id": "release-owner", "is_admin": True},
    )["global_hold"]

    with pytest.raises(AutomationError, match="automation_global_hold_ownership_lost"):
        request(
            service,
            "set_global_hold",
            {"enabled": True, "reason": reason},
            idempotency_key="reclassify-release-hold-0001",
            expected_revision=held["revision"],
            actor={"kind": "system", "id": actor_id, "is_admin": True},
        )

    current = request(service, "status")["global_hold"]
    assert {key: current[key] for key in held} == held


@pytest.mark.parametrize("operation,payload_kind", [("status", "exact"), ("status", "list"), ("readiness", "exact")])
def test_archived_job_supports_technical_readback_but_rejects_new_effects(tmp_path, operation, payload_kind):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    job = create_digest(service)
    archived = request(
        service,
        "archive",
        {"job_id": job["job_id"]},
        idempotency_key="archive-for-readback-0001",
        expected_revision=job["revision"],
    )
    payload = {"include_archived": True} if payload_kind == "list" else {"job_id": job["job_id"]}

    result = request(service, operation, payload)
    packet = result["readiness"]["execution_packet"] if operation == "status" else result["execution_packet"]
    assert packet["jobs"][0]["job_id"] == job["job_id"]
    if operation == "status":
        assert result["jobs"][0]["archived_at"] is not None
    with pytest.raises(AutomationError, match="automation_job_not_found"):
        request(
            service,
            "test_notification",
            {"job_id": job["job_id"]},
            idempotency_key="archived-effect-rejected-0001",
            expected_revision=archived["revision"],
        )


def test_notification_idempotency_is_scoped_by_source_and_preserves_command_replay(tmp_path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store)
    job = create_digest(service)
    payload = {"job_id": job["job_id"]}
    results = []
    for kind in ("codex", "telegram_owner"):
        actor = {"kind": kind, "id": "test-owner", "is_admin": True}
        result = request(
            service,
            "test_notification",
            payload,
            idempotency_key="shared-notification-key-0001",
            expected_revision=job["revision"],
            actor=actor,
        )
        replay = request(
            service,
            "test_notification",
            payload,
            idempotency_key="shared-notification-key-0001",
            expected_revision=job["revision"],
            actor=actor,
        )
        assert replay["outbox_id"] == result["outbox_id"]
        results.append(result)

    assert results[0]["outbox_id"] != results[1]["outbox_id"]
    with store.connect() as connection:
        keys = connection.execute("SELECT idempotency_key FROM manager_automation_outbox").fetchall()
    assert len(keys) == 2
    assert keys[0][0] != keys[1][0]


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
        actor={"kind": "system", "id": "release-test", "is_admin": True},
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
            actor={"kind": "system", "id": "release-test", "is_admin": True},
        )
    released = request(
        service,
        "set_global_hold",
        {"enabled": False, "reason": None, "attempt_hash": "d" * 64},
        idempotency_key="global-hold-0003",
        expected_revision=1,
        actor={"kind": "system", "id": "release-test", "is_admin": True},
    )
    assert released["global_hold"]["revision"] == 2
    assert store.claim_next_run(owner="worker-a") is not None


@pytest.mark.parametrize("kind", ["codex", "telegram_owner", "crm_operator"])
@pytest.mark.parametrize("operation", ["set_global_hold", "preview"])
def test_global_hold_control_is_release_system_only(tmp_path: Path, kind: str, operation: str):
    service = AutomationControlService(AutomationStore(tmp_path / "manager.sqlite3"))
    payload = {"enabled": True, "reason": "release", "attempt_hash": "d" * 64}
    if operation == "preview":
        payload = {"target_operation": "set_global_hold", "target_payload": payload}

    with pytest.raises(AutomationError, match="automation_permission_denied"):
        request(
            service,
            operation,
            payload,
            idempotency_key="global-hold-denied-0001" if operation != "preview" else None,
            expected_revision=0 if operation != "preview" else None,
            actor={"kind": kind, "id": "untrusted-hold-actor", "is_admin": True},
        )


@pytest.mark.parametrize("outcomes", [(False, False), (True, False, False, True, False)])
def test_last_success_survives_failures_and_advances_on_recovery(monkeypatch, tmp_path: Path, outcomes):
    current = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(automation_registry, "utc_now", lambda: current)
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = enable_digest(service, create_digest(service))
    expected_last_success = None

    for index, succeeded in enumerate(outcomes):
        request(
            service,
            "run_now",
            {"job_id": job["job_id"]},
            idempotency_key=f"success-history-run-{index:04d}",
            expected_revision=job["revision"],
        )
        claim = store.claim_next_run(owner="success-history-worker")
        assert claim
        assert store.finish_run(
            job_id=job["job_id"],
            run_id=claim["run_id"],
            owner="success-history-worker",
            fencing_token=claim["fencing_token"],
            succeeded=succeeded,
            result_code="ok" if succeeded else "automation_crm_transport_failed",
        )
        if succeeded:
            expected_last_success = automation_registry.isoformat(current)
        with sqlite3.connect(path) as connection:
            actual_last_success = connection.execute(
                "SELECT last_success_at FROM manager_automation_runtime WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()[0]
            latest_successful_run = connection.execute(
                "SELECT MAX(finished_at) FROM manager_automation_runs WHERE job_id = ? AND status = 'succeeded'",
                (job["job_id"],),
            ).fetchone()[0]
        assert actual_last_success == latest_successful_run == expected_last_success
        assert store.status(job_id=job["job_id"])["jobs"][0]["last_success_at"] == expected_last_success
        current += timedelta(minutes=1)


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


def test_registry_helpers_reject_invalid_typed_values_and_normalize_naive_time():
    parsed = automation_registry.parse_time("2026-09-21T12:00:00")
    assert parsed == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert automation_registry.parse_time("not-a-time") is None
    assert automation_registry._decode_json("not-json", {"fallback": True}) == {"fallback": True}
    assert is_in_active_window(datetime(2026, 9, 21, tzinfo=UTC), timezone="UTC", active_window="24/7") is True

    for invalid in (None, 1, "Unknown/Nowhere"):
        with pytest.raises(AutomationError, match="automation_timezone_invalid"):
            automation_registry.normalize_timezone(invalid)
    for invalid in (None, {}, {"start": "09:00", "end": "09:00"}):
        with pytest.raises(AutomationError, match="automation_active_window_invalid"):
            automation_registry.normalize_active_window(invalid)
    with pytest.raises(AutomationError, match="automation_next_delay_invalid"):
        next_scheduled_time(
            datetime(2026, 9, 21, tzinfo=UTC),
            delay_seconds=-1,
            timezone="UTC",
            active_window="24/7",
        )


def test_registry_storage_rejects_unsafe_paths_and_newer_schema(tmp_path: Path):
    with pytest.raises(AutomationError, match="automation_db_path_invalid"):
        with AutomationStore(Path("relative.sqlite3")).connect():
            pass

    open_parent = tmp_path / "open"
    open_parent.mkdir(mode=0o755)
    open_parent.chmod(0o755)
    with pytest.raises(AutomationError, match="automation_db_directory_invalid"):
        with AutomationStore(open_parent / "manager.sqlite3").connect():
            pass

    secure_parent = tmp_path / "secure"
    secure_parent.mkdir(mode=0o700)
    directory_at_file = secure_parent / "manager.sqlite3"
    directory_at_file.mkdir()
    with pytest.raises(AutomationError, match="automation_db_file_invalid"):
        with AutomationStore(directory_at_file).connect():
            pass

    newer_path = tmp_path / "newer.sqlite3"
    with sqlite3.connect(newer_path) as connection:
        connection.execute(
            "CREATE TABLE manager_automation_schema(component TEXT PRIMARY KEY, version INTEGER, upgraded_at TEXT)"
        )
        connection.execute(
            "INSERT INTO manager_automation_schema(component, version, upgraded_at) VALUES('automation_center', 999, '')"
        )
    newer_path.chmod(0o600)
    with pytest.raises(AutomationError, match="automation_schema_newer_than_runtime"):
        AutomationStore(newer_path).initialize()

    valid = AutomationStore(tmp_path / "valid.sqlite3")
    assert valid.schema_version() == automation_registry.AUTOMATION_SCHEMA_VERSION


def test_command_replay_rejects_invalid_key_and_corrupt_stored_result(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    with pytest.raises(AutomationError, match="idempotency_key_invalid"):
        store.execute_command(
            source="codex",
            idempotency_key="short",
            operation="status",
            request_hash="request",
            actor_hash="actor",
            mutation=lambda _connection, _command_id: {},
        )

    store.initialize()
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO manager_automation_commands(
                command_id, source, idempotency_key, operation, request_hash,
                actor_hash, response_json, created_at
            ) VALUES('cmd_corrupt', 'codex', 'corrupt-result-0001', 'status', 'request', 'actor', '[]', '')
            """
        )
    with pytest.raises(AutomationError, match="idempotency_result_invalid"):
        store.execute_command(
            source="codex",
            idempotency_key="corrupt-result-0001",
            operation="status",
            request_hash="request",
            actor_hash="actor",
            mutation=lambda _connection, _command_id: pytest.fail("corrupt replay must not mutate"),
        )


def test_global_hold_validations_and_owner_binding(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    store.initialize()
    with store.connect() as connection:
        invalid = [
            (
                {"enabled": "yes", "reason": None, "attempt_hash": None, "expected_revision": 0},
                "automation_global_hold_invalid",
            ),
            (
                {"enabled": True, "reason": "operator", "attempt_hash": None, "expected_revision": None},
                "expected_revision_required",
            ),
            (
                {"enabled": True, "reason": "unknown", "attempt_hash": None, "expected_revision": 0},
                "automation_global_hold_reason_invalid",
            ),
            (
                {"enabled": True, "reason": "release", "attempt_hash": "bad", "expected_revision": 0},
                "automation_global_hold_attempt_invalid",
            ),
            (
                {"enabled": True, "reason": "release", "attempt_hash": None, "expected_revision": 0},
                "automation_global_hold_attempt_required",
            ),
            (
                {"enabled": True, "reason": "operator", "attempt_hash": "a" * 64, "expected_revision": 0},
                "automation_global_hold_attempt_invalid",
            ),
            (
                {"enabled": False, "reason": "operator", "attempt_hash": None, "expected_revision": 0},
                "automation_global_hold_reason_invalid",
            ),
        ]
        for values, code in invalid:
            with pytest.raises(AutomationError, match=code):
                store.set_global_hold(connection, actor_hash="actor-a", **values)

        held = store.set_global_hold(
            connection,
            enabled=True,
            reason="release",
            attempt_hash="a" * 64,
            expected_revision=0,
            actor_hash="actor-a",
        )
        assert held["global_hold"]["revision"] == 1
        with pytest.raises(AutomationError, match="automation_global_hold_ownership_lost"):
            store.set_global_hold(
                connection,
                enabled=False,
                reason=None,
                attempt_hash="a" * 64,
                expected_revision=1,
                actor_hash="actor-b",
            )
        with pytest.raises(AutomationError, match="automation_global_hold_ownership_lost"):
            store.set_global_hold(
                connection,
                enabled=True,
                reason="release",
                attempt_hash="a" * 64,
                expected_revision=1,
                actor_hash="actor-b",
            )
        replay = store.set_global_hold(
            connection,
            enabled=True,
            reason="release",
            attempt_hash="a" * 64,
            expected_revision=1,
            actor_hash="actor-a",
        )
        assert replay["changed"] is False


def test_system_timer_and_delivery_state_validations(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    store.initialize()
    with pytest.raises(AutomationError, match="system_timer_control_mode_invalid"):
        store.adopt_system_timer(timer_id="test", unit_name="test.timer", control_mode="unsafe", state={})

    state = {"desired_state": "on", "actual_state": "active", "period_minutes": 15}
    store.adopt_system_timer(
        timer_id="managed_pc_health",
        unit_name="autostop-managed-pc-health.timer",
        control_mode="managed",
        state=state,
    )
    with pytest.raises(AutomationError, match="system_timer_policy_changed"):
        store.adopt_system_timer(
            timer_id="managed_pc_health",
            unit_name="different.timer",
            control_mode="managed",
            state=state,
        )

    with store.connect() as connection:
        for method, values, code in [
            (
                store.update_system_timer,
                {
                    "timer_id": "missing",
                    "unit_name": "missing.timer",
                    "control_mode": "managed",
                    "state": state,
                    "expected_revision": 1,
                },
                "system_timer_not_adopted",
            ),
            (
                store.update_system_timer,
                {
                    "timer_id": "managed_pc_health",
                    "unit_name": "autostop-managed-pc-health.timer",
                    "control_mode": "managed",
                    "state": state,
                    "expected_revision": None,
                },
                "expected_revision_required",
            ),
            (
                store.require_system_timer_revision,
                {"timer_id": "managed_pc_health", "expected_revision": 2},
                "automation_revision_conflict",
            ),
        ]:
            with pytest.raises(AutomationError, match=code):
                method(connection, **values)

    for operation in (
        lambda: store.controller_heartbeat(owner="worker", state="invalid"),
        lambda: store.cancel_outbox_claim(outbox_id="out", owner="worker", fencing_token=1, result_code="BAD"),
        lambda: store.block_outbox_claim(outbox_id="out", owner="worker", fencing_token=1, result_code="BAD"),
        lambda: store.finish_outbox(
            outbox_id="out", owner="worker", fencing_token=1, sent=False, result_code="ok", retry_seconds=1
        ),
    ):
        with pytest.raises(AutomationError):
            operation()
