import importlib.util
import json
import os
from contextlib import contextmanager, nullcontext
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
        self.start_count = 0
        self.auto_restart = False
        self.boot_id = "boot1"
        self.daemon_id = "daemon1"
        self.receipt = {"pid": 7, "start_ticks": 123, "generation": "d" * 32, "attempt": "", "state": "idle"}

    def boot(self):
        return self.boot_id

    def daemon(self):
        return self.daemon_id

    def crm(self):
        return self.c.copy()

    def scheduler(self):
        return self.s.copy()

    def control_enabled(self, identity):
        return identity == self.c["id"]

    def restart_policy(self, identity):
        assert identity == self.c["id"]
        return "always"

    def prepare(self, identity, expected):
        assert identity == self.c["id"]
        assert expected["started"] == self.c["started"]
        self.receipt["attempt"] = "e" * 32
        self.receipt["state"] = "ready"
        return self.receipt.copy()

    def commit(self, identity, ready):
        assert identity == self.c["id"] and ready["state"] == "ready"
        self.receipt["state"] = "committed"
        return self.receipt.copy()

    def control(self, identity, operation="read", expected=None):
        assert identity == self.c["id"]
        if operation == "TERM":
            assert expected == self.receipt and self.receipt["state"] == "committed"
            self.actions.append(("term", "crm"))
            if self.auto_restart:
                self.c.update(started="start2", restarts="1", state="running", health="healthy")
                self.receipt.update(generation="f" * 32, attempt="", state="idle")
        return self.receipt.copy()

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
            self.start_count += 1
            self.c.update(state="running", started="start" + str(self.start_count + 1), health="healthy")
            self.receipt.update(generation=("f" if self.start_count % 2 else "d") * 32, attempt="", state="idle")
        else:
            self.s.update(state="active")

    def launch(self, attempt, operation, kind):
        m.worker(self.root, attempt, operation, kind, self)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # Only synthetic test files are owned by the runner instead of root.
    monkeypatch.setattr(m, "ROOT_UID", os.geteuid())
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
    monkeypatch.setattr(m, "host_writer_fence", lambda *_args, **_kwargs: nullcontext())
    return root, data, status, fake, tmp_path / "deploy.lock"


def run(ctx):
    root, data, status, fake, lock = ctx
    return m.backup(root, status, fake, lock, data, "b" * 40)


def test_private_state_requires_owner_and_private_mode(tmp_path, monkeypatch):
    assert m.ROOT_UID == 0
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    snapshot = root / "snapshot"
    snapshot.write_bytes(b"synthetic")
    snapshot.chmod(0o600)
    lock = root / "lock"
    lock.touch(mode=0o600)

    monkeypatch.setattr(m, "ROOT_UID", os.geteuid() + 1)
    with pytest.raises(m.Uncertain, match="root-private"):
        m.make_root(root)
    with pytest.raises(m.Uncertain, match="unsafe lock"):
        with m.Lock(lock):
            pass
    with snapshot.open("rb") as stream, pytest.raises(m.Uncertain, match="unsafe snapshot"):
        m._checked_stat(stream)

    monkeypatch.setattr(m, "ROOT_UID", os.geteuid())
    root.chmod(0o755)
    with pytest.raises(m.Uncertain, match="root-private"):
        m.make_root(root)
    root.chmod(0o700)
    lock.chmod(0o644)
    with pytest.raises(m.Uncertain, match="unsafe lock"):
        with m.Lock(lock):
            pass
    snapshot.chmod(0o644)
    with snapshot.open("rb") as stream, pytest.raises(m.Uncertain, match="unsafe snapshot"):
        m._checked_stat(stream)


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


def test_host_fence_releases_before_crm_restart(ctx, monkeypatch):
    _, _, _, fake, _ = ctx
    events = []

    @contextmanager
    def fence(_data):
        assert fake.crm()["state"] == "exited"
        events.append("fenced")
        try:
            yield
        finally:
            events.append("released")

    original = fake.start

    def start(kind, identity):
        if kind == "crm":
            assert events == ["fenced", "released"]
        original(kind, identity)

    monkeypatch.setattr(m, "host_writer_fence", fence)
    monkeypatch.setattr(fake, "start", start)
    run(ctx)
    assert events == ["fenced", "released"]


def test_host_fence_failure_skips_copy_and_restores_services(ctx, monkeypatch):
    root, _, _, fake, _ = ctx

    @contextmanager
    def fence(_data):
        raise m.Uncertain("synthetic busy host writer")
        yield  # pragma: no cover

    monkeypatch.setattr(m, "host_writer_fence", fence)
    with pytest.raises(m.Uncertain, match="busy host writer"):
        run(ctx)
    assert fake.crm()["state"] == "running" and fake.scheduler()["state"] == "active"
    assert not (root / "pending.json").exists()


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
    assert f"Последняя передача другой копии: {delivery['id']}; успех" in status.read_text()
    delivery["id"] = result["attempt"]
    m.save(root / "delivery-status.json", delivery)
    m.refresh(root, status)
    assert "Передача на ПК: успех" in status.read_text()


def test_failed_attempt_reports_delivery_of_previous_pending_copy(ctx):
    root, data, status, fake, lock = ctx
    previous = run(ctx)
    with pytest.raises(m.Uncertain):
        m.backup(root, status, fake, lock, data, "")
    m.save(
        root / "delivery-status.json",
        {"id": previous["attempt"], "status": "ok", "at": "2026-10-31T00:00:00Z", "error_code": None},
    )
    m.refresh(root, status)
    text = status.read_text()
    assert "Копия: ошибка" in text
    assert "Передача на ПК: не требовалась" in text
    assert f"Последняя передача другой копии: {previous['attempt']}; успех; время: 2026-10-31T00:00:00Z" in text


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
            "revision": fake.c["revision"],
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


def test_control_disabled_never_reads_receipt_or_sends_signal():
    class Disabled(m.System):
        def control_enabled(self, identity):
            return False

        def control(self, *args):
            raise AssertionError("control must not be called")

    with pytest.raises(m.PrepareAborted):
        Disabled().prepare("a" * 64, {})


def test_changed_generation_before_prepare_never_sends_signal():
    class Changed(m.System):
        def control_enabled(self, identity):
            return True

        def control(self, identity, operation="read", expected=None):
            assert operation == "read"
            return {"pid": 7, "start_ticks": 123, "generation": "d" * 32, "attempt": "", "state": "idle"}

        def crm(self):
            return {"id": "a" * 64, "state": "running", "started": "changed", "restarts": "0", "revision": "b" * 40}

    with pytest.raises(m.PrepareAborted):
        Changed().prepare(
            "a" * 64,
            {
                "started": "old",
                "restarts": "0",
                "revision": "b" * 40,
                "control": {"pid": 7, "start_ticks": 123, "generation": "d" * 32},
            },
        )


def test_aborted_prepare_never_stops_crm_and_restores_scheduler(ctx):
    root, _, _, fake, _ = ctx

    def abort(*_):
        raise m.PrepareAborted("synthetic refusal")

    fake.prepare = abort
    with pytest.raises(m.Uncertain):
        run(ctx)
    assert fake.actions == [("stop", "scheduler"), ("start", "scheduler")]
    assert fake.crm()["state"] == "running" and not (root / "pending.json").exists()


def test_unknown_commit_never_sends_stop(ctx):
    root, _, _, fake, _ = ctx

    def unknown(*_):
        raise m.Uncertain("synthetic unknown commit")

    fake.commit = unknown
    with pytest.raises(m.Uncertain):
        run(ctx)
    assert ("stop", "crm") not in fake.actions and not (root / "pending.json").exists()
    assert m.load(root / "attempt.json")["phase"] == "crm_commit_unknown"


def test_forced_exit_after_commit_rejects_snapshot_but_restores(ctx):
    root, _, _, fake, _ = ctx
    old_stop = fake.stop

    def forced(kind, identity):
        old_stop(kind, identity)
        if kind == "crm":
            fake.c["exit"] = "137"

    fake.stop = forced
    with pytest.raises(m.Uncertain):
        run(ctx)
    assert not (root / "pending.json").exists()
    assert fake.crm()["state"] == "running" and fake.scheduler()["state"] == "active"


def test_daemon_restart_with_inflight_stop_and_same_committed_crm_does_not_claim_recovery(ctx):
    root, _, status, fake, _ = ctx
    fake.daemon_id = "daemon2"
    fake.receipt.update(attempt="e" * 32, state="committed")
    fake.s["state"] = "inactive"
    record = {
        "attempt": "1" * 32,
        "phase": "crm_stop_unknown",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {
            "identity": fake.c["id"],
            "started": fake.c["started"],
            "restarts": fake.c["restarts"],
            "revision": fake.c["revision"],
            "initially_active": True,
            "phase": "untouched",
            "drain": dict(fake.receipt),
        },
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [] and m.load(root / "attempt.json")["restore"] == "unknown"


def test_recovery_does_not_start_service_stopped_by_another_actor(ctx):
    root, _, status, fake, _ = ctx
    fake.c["state"] = "exited"
    fake.s["state"] = "inactive"
    record = {
        "attempt": "1" * 32,
        "phase": "crm_prepare_aborted",
        "copy": "failed",
        "restore": "pending",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [] and fake.c["state"] == "exited"


def test_unknown_commit_recovers_only_exact_committed_pid(ctx):
    root, _, status, fake, _ = ctx
    fake.s["state"] = "inactive"
    ready = dict(fake.receipt, attempt="e" * 32, state="ready")
    fake.receipt.update(attempt="e" * 32, state="committed")
    record = {
        "attempt": "1" * 32,
        "phase": "crm_commit_unknown",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {
            "identity": fake.c["id"],
            "started": fake.c["started"],
            "restarts": fake.c["restarts"],
            "revision": fake.c["revision"],
            "initially_active": True,
            "phase": "untouched",
            "drain": ready,
        },
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    m.save(root / "attempt.json", record)
    fake.auto_restart = True
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [("term", "crm")]
    assert m.load(root / "attempt.json")["phase"] == "crm_term_intent"
    out = m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [("term", "crm"), ("start", "scheduler")]
    assert out["phase"] == "complete" and out["restore"] == "ok" and out["copy"] == "failed"


def test_unknown_term_is_never_repeated_for_different_generation(ctx):
    root, _, status, fake, _ = ctx
    ready = dict(fake.receipt, attempt="e" * 32, state="ready")
    fake.receipt.update(attempt="e" * 32, state="committed")
    record = {
        "attempt": "1" * 32,
        "phase": "crm_commit_unknown",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {
            "identity": fake.c["id"],
            "started": fake.c["started"],
            "restarts": fake.c["restarts"],
            "revision": fake.c["revision"],
            "initially_active": True,
            "phase": "untouched",
            "drain": ready,
        },
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    fake.s["state"] = "inactive"
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    fake.receipt["generation"] = "0" * 32
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [("term", "crm")]


def test_recovery_does_not_start_scheduler_stopped_by_another_actor(ctx):
    root, _, status, fake, _ = ctx
    fake.s["state"] = "inactive"
    record = {
        "attempt": "1" * 32,
        "phase": "crm_prepare_aborted",
        "copy": "failed",
        "restore": "pending",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {"identity": fake.c["id"], "initially_active": True, "phase": "untouched"},
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "untouched"},
    }
    m.save(root / "attempt.json", record)
    with pytest.raises(m.Uncertain):
        m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [] and fake.s["state"] == "inactive"


def test_result_fsync_failure_preserves_previous_archive_and_reports_unknown(ctx, monkeypatch):
    root, _, status, _, _ = ctx
    run(ctx)
    previous = m.load_pending(root / "pending.json")
    original = m.save

    def fail_after_replace(path, record):
        if path.name == "attempt.json" and record.get("copy") == "ok":
            original(path, record)
            raise OSError("synthetic post-replace fsync failure")
        return original(path, record)

    monkeypatch.setattr(m, "save", fail_after_replace)
    with pytest.raises(OSError):
        run(ctx)
    assert (root / previous["file"]).exists()
    assert m.load(root / "attempt.json")["copy"] == "unknown"
    assert "Копия: не подтверждён" in status.read_text()


def test_expired_exact_commit_returns_only_owned_scheduler_without_signal(ctx):
    root, _, status, fake, _ = ctx
    fake.s["state"] = "inactive"
    ready = dict(fake.receipt, attempt="e" * 32, state="ready")
    fake.receipt.update(attempt="e" * 32, state="aborted")
    record = {
        "attempt": "1" * 32,
        "phase": "crm_commit_unknown",
        "copy": "failed",
        "restore": "unknown",
        "delivery": "pending",
        "boot": "boot1",
        "daemon": "daemon1",
        "crm": {
            "identity": fake.c["id"],
            "started": fake.c["started"],
            "restarts": fake.c["restarts"],
            "revision": fake.c["revision"],
            "initially_active": True,
            "phase": "untouched",
            "drain": ready,
        },
        "scheduler": {"identity": fake.s["identity"], "initially_active": True, "phase": "stopped"},
    }
    m.save(root / "attempt.json", record)
    out = m.recover(root, status, fake, interrupted=True)
    assert fake.actions == [("start", "scheduler")]
    assert out["phase"] == "complete" and out["restore"] == "ok" and out["copy"] == "failed"


def test_status_failure_cannot_expose_copy_success_to_sender(ctx, monkeypatch):
    import threading

    root, _, status, _, _ = ctx
    run(ctx)
    previous = m.load_pending(root / "pending.json")
    original = m.refresh
    entered, release = threading.Event(), threading.Event()
    errors = []

    def fail_final_refresh(root_path, status_path, *, copy_override=None):
        if copy_override == "ok":
            entered.set()
            assert release.wait(5)
            raise OSError("synthetic M2 status failure")
        return original(root_path, status_path, copy_override=copy_override)

    def run_backup():
        try:
            run(ctx)
        except Exception as exc:  # noqa: BLE001 - propagate worker failure to the main test.
            errors.append(exc)

    monkeypatch.setattr(m, "refresh", fail_final_refresh)
    thread = threading.Thread(target=run_backup)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(BlockingIOError):
            with m.Lock(root / "operation.lock", wait=False):
                pass
        assert m.load(root / "attempt.json")["copy"] == "running"
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], OSError)
    assert m.load(root / "attempt.json")["copy"] == "unknown"
    assert "Копия: не подтверждён" in status.read_text()
    assert (root / previous["file"]).exists()
