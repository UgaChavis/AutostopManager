"""Fault contracts for the one-time legacy handoff, using no real services."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "first_transition", Path(__file__).parents[1] / "scripts/crm-first-transition.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeSystem:
    def __init__(self):
        self.current = {
            "id": "a" * 64,
            "state": "running",
            "started": "generation-one",
            "finished": "never",
            "exit": "0",
            "oom": "false",
            "restarts": "0",
            "health": "healthy",
            "revision": m.REVISION,
        }
        self.actions = []
        self.boot_id = "boot-one"
        self.daemon_id = "daemon-one"
        self.blocked = False
        self.stop_error = None

    def crm(self):
        return self.current.copy()

    def boot(self):
        return self.boot_id

    def daemon(self):
        return self.daemon_id

    def preconditions(self):
        return {"synthetic-consumer": "active" if self.blocked else "inactive"}

    def validate(self):
        pass

    def capture(self, initial):
        self.actions.append("capture")
        return {"pid": 7, "start_ticks": 100}

    def freeze(self, value):
        self.actions.append("freeze")

    def gate(self, value):
        self.actions.append("gate")

    def rollback(self, value):
        self.actions.append("rollback")

    def thaw_after_term(self, value):
        self.actions.append("thaw")

    def launch_stop(self, identity):
        self.actions.append("stop")
        if self.stop_error:
            raise self.stop_error
        system = self

        class Process:
            def poll(self):
                return None

            def wait(self):
                system.current.update(state="exited", finished="end-one", health="")
                return 0

        return Process()

    def start(self, identity):
        self.actions.append("start")
        self.current.update(state="running", started="generation-two", health="healthy")

    def healthy(self, identity):
        assert identity == self.current["id"]
        assert self.current["state"] == "running" and self.current["health"] == "healthy"

    def backup_space(self, root):
        self.actions.append("space")

    def snapshot(self, output, initial, stopped):
        assert self.current["state"] == "exited"
        self.actions.append("snapshot")
        with output.open("xb") as stream:
            stream.write(b"synthetic first backup")
        output.chmod(0o600)
        self.actions.append("manager_companion")
        name = output.name.replace("crm-first-", "manager-first-", 1)
        return {"file": name, "size": 1, "sha256": "d" * 64, "receipt_file": name + ".receipt.json"}


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(m.common, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(m, "OWNER_UID", os.geteuid())
    monkeypatch.setattr(m, "DEPLOY_LOCK", tmp_path / "deploy.lock")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    system = FakeSystem()
    ident = "b" * 32
    record = {
        **m.plan(system),
        "attempt": ident,
        "phase": "prepared",
        "restore": "pending",
        "result": "pending",
    }
    m.common.save(root / "attempt.json", record)
    return root, ident, system


def test_confirmed_stop_returns_same_container_with_current_data(attempt):
    root, ident, system = attempt
    m.worker(root, ident, system)
    result = m.recover(root, ident, system)
    assert result["phase"] == "complete" and result["restore"] == "ok"
    assert result["result"] == "stop_and_return_verified"
    assert system.actions.count("stop") == system.actions.count("start") == 1
    assert system.actions.index("thaw") < system.actions.index("start")


@pytest.mark.parametrize("phase", ["stop_intent", "stop_running", "stop_unknown"])
def test_unknown_stop_is_not_replayed_or_overtaken(attempt, phase):
    root, ident, system = attempt
    record = m.load(root)
    m.save(root, record, phase=phase)
    with pytest.raises(m.Uncertain):
        m.recover(root, ident, system)
    assert system.actions == []
    assert m.load(root)["restore"] == "unknown"


def test_changed_stopped_receipt_never_starts_another_generation(attempt):
    root, ident, system = attempt
    record = m.load(root)
    system.current.update(state="exited", finished="end-one", health="")
    m.save(root, record, phase="stopped", stop_receipt=system.crm())
    system.current["finished"] = "a-later-stop"
    with pytest.raises(m.Uncertain, match="receipt changed"):
        m.recover(root, ident, system)
    assert system.actions == []


def test_late_worker_cannot_stop_after_recovery(attempt):
    root, ident, system = attempt
    m.recover(root, ident, system)
    m.worker(root, ident, system)
    assert system.actions == []


def test_busy_consumers_refuse_before_capture(attempt):
    root, ident, system = attempt
    system.blocked = True
    m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert system.actions == []
    assert m.load(root)["result"] == "preconditions_changed"


def test_runtime_change_never_replays_old_process_control(attempt):
    root, ident, system = attempt
    record = m.load(root)
    m.save(root, record, phase="stop_unknown")
    system.boot_id = "boot-two"
    system.current["started"] = "generation-two"
    result = m.recover(root, ident, system)
    assert result["restore"] == "ok"
    assert result["result"] == "interrupted_runtime_changed"
    assert system.actions == []


@pytest.mark.parametrize("failed_phase", ["stop_running", "stopped", "start_intent"])
def test_journal_failure_after_stop_request_cannot_strand_crm(attempt, monkeypatch, failed_phase):
    root, ident, system = attempt
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == failed_phase and not injected:
            injected = True
            raise OSError("synthetic durable-write failure")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    assert injected
    assert system.current["state"] == "running" and system.current["started"] == "generation-two"
    assert system.actions.count("start") == 1


def test_commit_intent_write_failure_rolls_back_without_sending_stop(attempt, monkeypatch):
    root, ident, system = attempt
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == "stop_intent" and not injected:
            injected = True
            raise OSError("synthetic durable-write failure before stop")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert injected
    assert "stop" not in system.actions
    assert "rollback" in system.actions
    assert system.current["started"] == "generation-one"


def test_known_stop_launch_failure_returns_precommit_gate(attempt):
    root, ident, system = attempt
    system.stop_error = FileNotFoundError("synthetic CLI unavailable")
    with pytest.raises((OSError, m.Uncertain)):
        m.worker(root, ident, system)
    m.recover(root, ident, system)
    assert "rollback" in system.actions and "thaw" not in system.actions
    assert system.actions.count("stop") == 1


def test_recovery_journal_failure_cannot_skip_confirmed_owned_return(attempt, monkeypatch):
    root, ident, system = attempt
    system.current.update(state="exited", finished="end-one", health="")
    m.save(root, m.load(root), phase="stopped", stop_receipt=system.crm())
    original_save = m.common.save
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if value.get("phase") == "start_intent" and not injected:
            injected = True
            raise OSError("synthetic recovery journal failure")
        return original_save(path, value)

    monkeypatch.setattr(m.common, "save", fail_once)
    with pytest.raises((OSError, m.Uncertain)):
        m.recover(root, ident, system)
    assert injected
    assert system.current["state"] == "running" and system.current["started"] == "generation-two"
    assert system.actions == ["start"]


def request_backup(root):
    m.save(root, m.load(root), backup_requested=True, copy_status="pending")


def test_first_backup_ready_only_after_healthy_return_and_archive_check(attempt, monkeypatch):
    root, ident, system = attempt
    request_backup(root)
    original_receipt = m.archive_receipt

    def checked_receipt(*args):
        assert system.current["state"] == "running" and system.current["health"] == "healthy"
        assert m.load(root)["copy_status"] == "running"
        assert "archive" not in m.load(root)
        return original_receipt(*args)

    monkeypatch.setattr(m, "archive_receipt", checked_receipt)
    m.worker(root, ident, system)
    result = m.load(root)
    assert m.success(result) and result["copy_status"] == "ok"
    assert result["archive"]["file"] == f"crm-first-{ident}.tar.gz"
    assert result["archive"]["size"] == len(b"synthetic first backup")
    assert system.actions.index("snapshot") < system.actions.index("start")
    assert system.actions.index("manager_companion") < system.actions.index("start")
    assert result["manager_companion"]["file"] == f"manager-first-{ident}.tar.gz"


def test_first_backup_failure_returns_crm_and_keeps_previous_copies(attempt, monkeypatch):
    root, ident, system = attempt
    request_backup(root)
    previous = root / ("crm-first-" + "c" * 32 + ".tar.gz")
    previous.write_bytes(b"previous completed copy")

    def failed_snapshot(*_args):
        raise m.Uncertain("synthetic snapshot failure")

    monkeypatch.setattr(system, "snapshot", failed_snapshot)
    with pytest.raises(m.Uncertain, match="snapshot failure"):
        m.worker(root, ident, system)
    result = m.load(root)
    assert result["restore"] == "ok" and result["copy_status"] == "failed"
    assert not m.success(result) and "archive" not in result
    assert system.current["state"] == "running" and system.actions.count("start") == 1
    assert previous.read_bytes() == b"previous completed copy"


def test_first_backup_receipt_failure_does_not_publish_readiness(attempt, monkeypatch):
    root, ident, system = attempt
    request_backup(root)

    def failed_receipt(*_args):
        raise OSError("synthetic archive fsync failure")

    monkeypatch.setattr(m, "archive_receipt", failed_receipt)
    with pytest.raises(OSError, match="archive fsync"):
        m.worker(root, ident, system)
    result = m.load(root)
    assert result["restore"] == "ok" and result["copy_status"] == "unknown"
    assert "archive" not in result and not m.success(result)
    assert m.archive_path(root, ident).exists() and system.current["state"] == "running"


def test_interrupted_first_copy_is_not_promoted_by_recovery(attempt):
    root, ident, system = attempt
    system.current.update(state="exited", finished="end-one", health="")
    m.save(
        root,
        m.load(root),
        phase="stopped",
        stop_receipt=system.crm(),
        backup_requested=True,
        copy_status="running",
    )
    m.archive_path(root, ident).write_bytes(b"unconfirmed bytes")
    result = m.recover(root, ident, system)
    assert result["restore"] == "ok" and result["copy_status"] == "unknown"
    assert not m.success(result) and "archive" not in result
    assert system.actions == ["start"]


def test_real_snapshot_call_keeps_manager_companion_inside_cold_boundary(attempt, monkeypatch):
    root, ident, fake = attempt
    fake.current.update(state="exited", finished="end-one", health="")
    stopped = fake.crm()
    system = m.System()
    monkeypatch.setattr(system, "crm", fake.crm)
    monkeypatch.setattr(m.common, "host_writer_fence", lambda *_a: nullcontext())
    calls = []

    def capture_run(args, **kwargs):
        assert fake.current["state"] == "exited"
        calls.append((args, kwargs))
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(b"synthetic archive")
        output.chmod(0o600)
        if str(m.CHECKPOINT) in args:
            receipt = {
                "ok": True,
                "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "isolated_extraction_removed": True,
                "sqlite_runtime": "3.51.3",
            }
            path = output.with_name(output.name + ".receipt.json")
            path.write_text(json.dumps(receipt))
            path.chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(m.subprocess, "run", capture_run)
    metadata = system.snapshot(m.archive_path(root, ident), stopped, stopped)
    assert str(m.common.SNAPSHOT) in calls[0][0] and str(m.CHECKPOINT) in calls[1][0]
    assert calls[1][1]["env"]["LD_LIBRARY_PATH"] == m.SQLITE_LIBRARY
    assert metadata["file"] == f"manager-first-{ident}.tar.gz"
    assert fake.current["state"] == "exited"


def test_companion_preserves_committed_wal_data_from_extra_manager_root(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("first_checkpoint", m.CHECKPOINT)
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    # This synthetic WAL is created by the runner's engine. Production must
    # separately enforce the qualified engine for the real Manager databases.
    monkeypatch.setattr(checkpoint, "REQUIRED_SQLITE_VERSION", sqlite3.sqlite_version)
    verify_databases = checkpoint.verify_databases

    def verify_committed_row(root):
        count = verify_databases(root)
        copied = next(root.rglob("manager.sqlite3"))
        with sqlite3.connect(copied) as restored:
            assert restored.execute("SELECT value FROM synthetic").fetchall() == [("committed",)]
        return count

    monkeypatch.setattr(checkpoint, "verify_databases", verify_committed_row)
    state = tmp_path / "state"
    state.mkdir()
    additional = tmp_path / "actual-manager-data"
    additional.mkdir()
    connection = sqlite3.connect(additional / "manager.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE synthetic (value TEXT)")
        connection.execute("INSERT INTO synthetic VALUES ('committed')")
        connection.commit()
        output = tmp_path / "manager-first-test.tar.gz"
        tmp_path.chmod(0o700)
        checkpoint.create_archive((state, additional), output, excluded=set())
        receipt = checkpoint.verify_archive(output)
        assert receipt["ok"] and receipt["sqlite_databases_verified"] == 1
        assert receipt["isolated_extraction_removed"]
    finally:
        connection.close()


def test_companion_rejects_unqualified_sqlite_before_copy(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("first_checkpoint", m.CHECKPOINT)
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    monkeypatch.setattr(checkpoint.sqlite3, "sqlite_version", "0.0.0")
    monkeypatch.setattr(checkpoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(m.sys, "argv", [str(m.CHECKPOINT), "--output", str(tmp_path / "unused.tar.gz")])

    def unexpected_copy(*_args):
        pytest.fail("copy started with unqualified SQLite")

    monkeypatch.setattr(checkpoint, "create_archive", unexpected_copy)
    assert checkpoint.main() == 1
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="qualified SQLite"):
        checkpoint.verify_databases(tmp_path)
