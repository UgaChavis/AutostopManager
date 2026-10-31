import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("coord", Path(__file__).parents[1] / "scripts/crm_backup_coordinator.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Fake:
    def __init__(self, root):
        self.root = root
        self.c = {
            "id": "a" * 64,
            "state": "running",
            "started": "start1",
            "finished": "never",
            "exit": "0",
            "oom": "false",
            "restarts": "0",
            "health": "healthy",
            "revision": "b" * 40,
        }
        self.s = {"identity": "c" * 64, "state": "active", "invocation": "one"}
        self.actions = []
        self.boot_id = "boot1"
        self.daemon_id = "daemon1"

    def boot(self):
        return self.boot_id

    def daemon(self):
        return self.daemon_id

    def crm(self):
        return self.c.copy()

    def scheduler(self):
        return self.s.copy()

    def healthy(self, kind, identity):
        current = self.crm() if kind == "crm" else self.scheduler()
        assert (current["id"] if kind == "crm" else current["identity"]) == identity
        assert current["state"] == ("running" if kind == "crm" else "active")

    def stop(self, kind, identity):
        self.actions.append(("stop", kind))
        if kind == "crm":
            self.c.update(state="exited", finished="end1", health="")
        else:
            self.s.update(state="inactive")

    def start(self, kind, identity):
        self.actions.append(("start", kind))
        if kind == "crm":
            self.c.update(state="running", started="start2", health="healthy")
        else:
            self.s.update(state="active")

    def launch(self, attempt, operation, kind):
        m.worker(self.root, attempt, operation, kind, self)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    data = tmp_path / "data"
    data.mkdir()
    (data / "state").write_bytes(b"dummy")
    status = tmp_path / "status.md"
    fake = Fake(root)

    def snapshot(path, initial):
        assert fake.crm()["state"] == "exited"
        path.write_bytes(b"synthetic snapshot")
        path.chmod(0o600)

    monkeypatch.setattr(m, "snapshot_command", snapshot)
    return root, data, status, fake, tmp_path / "deploy.lock"


def run(ctx):
    root, data, status, fake, lock = ctx
    return m.backup(root, status, fake, lock, data, "b" * 40)


def test_backup_restores_before_publishing_and_preserves_one_pending(ctx, monkeypatch):
    root, _, status, fake, _ = ctx
    original_publish = m.publish

    def checked_publish(*args):
        assert fake.crm()["state"] == "running" and fake.scheduler()["state"] == "active"
        return original_publish(*args)

    monkeypatch.setattr(m, "publish", checked_publish)
    first = run(ctx)
    old = m.load_pending(root / "pending.json")
    fake.c.update(started="start2", finished="end1")
    second = run(ctx)
    new = m.load_pending(root / "pending.json")
    assert first["copy"] == second["copy"] == "ok"
    assert old["id"] != new["id"] and not (root / old["file"]).exists()
    assert (root / new["file"]).read_bytes() == b"synthetic snapshot"
    assert fake.actions[:4] == [("stop", "scheduler"), ("stop", "crm"), ("start", "crm"), ("start", "scheduler")]
    assert "Копия: успех" in status.read_text() and "Передача на ПК: ожидается" in status.read_text()


def test_failure_retains_old_pending_and_restores_services(ctx, monkeypatch):
    root, _, _, fake, _ = ctx
    run(ctx)
    old = m.load_pending(root / "pending.json")

    def fail(*_):
        raise RuntimeError("synthetic fault")

    monkeypatch.setattr(m, "snapshot_command", fail)
    with pytest.raises(RuntimeError):
        run(ctx)
    assert m.load_pending(root / "pending.json") == old and (root / old["file"]).exists()
    assert fake.crm()["state"] == "running" and fake.scheduler()["state"] == "active"


def test_restart_during_snapshot_rejects_copy_and_restores(ctx, monkeypatch):
    root, _, _, fake, _ = ctx

    def changed(path, initial):
        path.write_bytes(b"synthetic")
        fake.c.update(started="changed", restarts="1")

    monkeypatch.setattr(m, "snapshot_command", changed)
    with pytest.raises(m.Uncertain):
        run(ctx)
    assert not (root / "pending.json").exists()
    assert fake.crm()["state"] == "running" and fake.scheduler()["state"] == "active"


def test_initially_inactive_scheduler_not_started(ctx):
    _, _, _, fake, _ = ctx
    fake.s.update(state="inactive", invocation="")
    run(ctx)
    assert fake.s["state"] == "inactive"
    assert all(kind != "scheduler" for _, kind in fake.actions)


def test_late_worker_after_recovery_cannot_stop_service(ctx):
    root, _, status, fake, _ = ctx
    attempt = "1" * 32
    record = {
        "attempt": attempt,
        "boot": "boot1",
        "daemon": "daemon1",
        "phase": "crm_stop_intent",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "untouched"},
    }
    m.save(root / "attempt.json", record)
    m.recover(root, status, fake)
    m.worker(root, attempt, "stop", "crm", fake)
    assert fake.crm()["state"] == "running" and not fake.actions


def test_killed_worker_is_unknown_without_daemon_restart(ctx):
    root, _, status, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "boot": "boot1",
        "daemon": "daemon1",
        "phase": "crm_stop_running",
        "copy": "failed",
        "restore": "pending",
        "delivery": "pending",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    fake.s["state"] = "inactive"
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake)
    assert fake.actions == [] and "Возврат служб: не подтверждён" in status.read_text()
    fake.boot_id = "boot2"
    m.recover(root, status, fake)
    assert fake.actions == [("start", "scheduler")]


def test_pointer_tampering_does_not_delete_existing_copy(ctx):
    root, _, _, _, _ = ctx
    run(ctx)
    old = m.load_pending(root / "pending.json")
    data = json.loads((root / "pending.json").read_text())
    data["file"] = "../do-not-touch"
    (root / "pending.json").write_text(json.dumps(data))
    with pytest.raises(m.Uncertain):
        run(ctx)
    assert (root / old["file"]).exists()


def test_unapproved_live_revision_is_rejected_before_any_stop(ctx):
    root, data, status, fake, lock = ctx
    with pytest.raises(m.Uncertain):
        m.backup(root, status, fake, lock, data, "")
    result = m.load(root / "attempt.json")
    assert not fake.actions and result["copy"] == "failed" and result["restore"] == "not_needed"
    assert "Копия: ошибка" in status.read_text()


def test_daemon_restart_does_not_resolve_scheduler_unknown(ctx):
    root, _, status, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "boot": "boot1",
        "daemon": "daemon1",
        "phase": "scheduler_stop_unknown",
        "copy": "failed",
        "restore": "pending",
        "delivery": "pending",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "untouched"},
    }
    fake.daemon_id = "daemon2"
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake)
    assert not fake.actions


def test_reboot_with_initially_inactive_but_now_active_scheduler_is_not_success(ctx):
    root, _, status, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "boot": "boot1",
        "daemon": "daemon1",
        "phase": "prepared",
        "copy": "failed",
        "restore": "pending",
        "delivery": "pending",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": False, "phase": "untouched"},
    }
    fake.boot_id = "boot2"
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake)
    assert json.loads((root / "attempt.json").read_text())["restore"] != "ok"


def test_delivery_status_does_not_cross_attempts(ctx):
    root, _, status, _, _ = ctx
    result = run(ctx)
    delivery = {"id": "9" * 32, "status": "ok", "at": "2026-10-31T00:00:00Z", "error_code": None}
    m.save(root / "delivery-status.json", delivery)
    m.refresh(root, status)
    assert "Передача на ПК: ожидается" in status.read_text()
    delivery["id"] = result["attempt"]
    m.save(root / "delivery-status.json", delivery)
    m.refresh(root, status)
    assert "Передача на ПК: успех" in status.read_text()


def test_journal_update_waits_for_inflight_worker(ctx):
    import threading

    root, _, _, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "boot": "boot1",
        "daemon": "daemon1",
        "phase": "crm_stop_intent",
        "copy": "running",
        "restore": "pending",
        "delivery": "pending",
        "crm": {
            "identity": fake.c["id"],
            "started": fake.c["started"],
            "restarts": fake.c["restarts"],
            "initially_active": True,
            "phase": "untouched",
        },
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "untouched"},
    }
    m.save(root / "attempt.json", record)
    entered, release, updated = threading.Event(), threading.Event(), threading.Event()
    original = fake.stop

    def stop(kind, identity):
        entered.set()
        assert release.wait(5)
        original(kind, identity)

    fake.stop = stop
    worker = threading.Thread(target=m.worker, args=(root, record["attempt"], "stop", "crm", fake))
    worker.start()
    assert entered.wait(5)

    def amend():
        m.amend(root, record["attempt"], {"copy": "failed"})
        updated.set()

    editor = threading.Thread(target=amend)
    editor.start()
    assert not updated.wait(0.1)
    release.set()
    worker.join(5)
    editor.join(5)
    saved = m.load(root / "attempt.json")
    assert updated.is_set() and saved["phase"] == "crm_stop_done" and saved["copy"] == "failed"


def test_completed_attempt_never_restarts_services(ctx):
    root, _, status, fake, _ = ctx
    result = run(ctx)
    fake.c["state"] = "exited"
    fake.s["state"] = "inactive"
    fake.actions.clear()
    fake.boot_id = "boot2"
    recovered = m.recover(root, status, fake, interrupted=True)
    assert recovered["attempt"] == result["attempt"] and not fake.actions
    assert fake.c["state"] == "exited" and fake.s["state"] == "inactive"


def test_interrupted_copy_after_completed_recovery_is_unknown(ctx):
    root, _, status, fake, _ = ctx
    result = run(ctx)
    m.amend(root, result["attempt"], {"copy": "running"})
    fake.actions.clear()
    recovered = m.recover(root, status, fake, interrupted=True)
    assert recovered["copy"] == "unknown" and not fake.actions
    assert "Копия: не подтверждён" in status.read_text()


def test_preflight_failure_replaces_previous_success_in_status(ctx):
    root, data, status, fake, lock = ctx
    prior = run(ctx)
    with pytest.raises(m.Uncertain):
        m.backup(root, status, fake, lock, data, "")
    current = m.load(root / "attempt.json")
    assert current["attempt"] != prior["attempt"] and current["copy"] == "failed"
    assert "Копия: ошибка" in status.read_text()


def test_old_archive_hash_does_not_hold_pending_lock(ctx, monkeypatch):
    import os
    import threading

    root, _, _, _, _ = ctx
    run(ctx)
    old = m.load_pending(root / "pending.json")
    old_inode = (root / old["file"]).stat().st_ino
    path = root / ".staged-new.tar.gz"
    path.write_bytes(b"new synthetic snapshot")
    path.chmod(0o600)
    hashing, release = threading.Event(), threading.Event()
    original = m._file_info
    errors = []

    def slowed(stream):
        if os.fstat(stream.fileno()).st_ino == old_inode:
            hashing.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
        return original(stream)

    monkeypatch.setattr(m, "_file_info", slowed)

    def publish():
        try:
            m.publish(root, path, "2" * 32)
        except Exception as exc:  # noqa: BLE001 - propagate worker failure to the main test.
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert hashing.wait(5)
        with m.Lock(root / "pending.lock", wait=False):
            assert m.load_pending(root / "pending.json") == old
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert m.load_pending(root / "pending.json")["id"] == "2" * 32


def test_changed_old_file_is_never_removed(ctx, monkeypatch):
    root, _, _, _, _ = ctx
    run(ctx)
    old = m.load_pending(root / "pending.json")
    path = root / ".staged-new.tar.gz"
    path.write_bytes(b"new synthetic snapshot")
    path.chmod(0o600)
    original = m._publish_locked

    def changed(root, path, attempt, staged_info, digest, old_value, old_info):
        (root / old_value["file"]).write_bytes(b"changed while publisher was hashing")
        return original(root, path, attempt, staged_info, digest, old_value, old_info)

    monkeypatch.setattr(m, "_publish_locked", changed)
    m.publish(root, path, "2" * 32)
    assert (root / old["file"]).read_bytes() == b"changed while publisher was hashing"
    assert m.load_pending(root / "pending.json")["id"] == "2" * 32


def test_ready_failure_preserves_previous_archive_and_blocks_unconfirmed_pointer(ctx, monkeypatch):
    root, _, _, _, _ = ctx
    run(ctx)
    old = m.load_pending(root / "pending.json")
    assert m.load_pending(root / "pending-ready.json") == old
    original = m.save

    def fail_ready(path, record):
        if path.name == "pending-ready.json":
            raise OSError("synthetic fsync failure")
        return original(path, record)

    monkeypatch.setattr(m, "save", fail_ready)
    with pytest.raises(OSError):
        run(ctx)
    pointer = m.load_pending(root / "pending.json")
    ready = m.load_pending(root / "pending-ready.json")
    assert pointer != ready and ready == old and (root / old["file"]).exists()
    assert m.load(root / "attempt.json")["copy"] == "unknown"


def test_recovery_can_retry_without_waiting_for_active_worker(ctx):
    root, _, status, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "phase": "prepared",
        "copy": "pending",
        "restore": "pending",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
    }
    m.save(root / "attempt.json", record)
    with m.Lock(root / "operation.lock"):
        with pytest.raises(BlockingIOError):
            m.recover(root, status, fake, interrupted=True, wait=False)
    assert m.load(root / "attempt.json") == record and not fake.actions


def test_repeated_unknown_recovery_does_not_rewrite_journal(ctx):
    root, _, status, fake, _ = ctx
    record = {
        "attempt": "1" * 32,
        "phase": "crm_stop_unknown",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
    }
    m.save(root / "attempt.json", record)
    before = (root / "attempt.json").stat().st_mtime_ns
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True, wait=False)
    assert m.load(root / "attempt.json") == record
    assert (root / "attempt.json").stat().st_mtime_ns == before
