import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deliver-crm-backup.py"
spec = importlib.util.spec_from_file_location("crm_backup_delivery", SCRIPT)
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class FakeWindows:
    def __init__(self):
        self.parts = {}
        self.finals = {}
        self.writes = []
        self.moves = []
        self.deletes = []
        self.fail_write = False
        self.fail_move_after_effect = False
        self.busy = False
        self.bad_inspection = None
        self.on_final_verify = None
        self.on_part_verify = None
        self.inspections = []

    @staticmethod
    def info(data):
        if data is None:
            return None
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def inspect(self, record):
        self.inspections.append(record["id"])
        if self.busy:
            raise delivery.DeliveryError("ssh_unknown", unknown=True)
        final = self.info(self.finals.get(record["id"]))
        if record["id"] == self.bad_inspection:
            final = self.info(b"unexpected bytes")
        return {"part": self.info(self.parts.get(record["id"])), "final": final}

    def write(self, record, stream):
        ident = record["id"]
        self.writes.append(ident)
        data = stream.read()
        if self.fail_write:
            self.parts[ident] = data[:2]
            self.fail_write = False
            raise delivery.DeliveryError("ssh_unknown", unknown=True)
        assert ident not in self.parts
        self.parts[ident] = data

    def verify(self, record, *, final):
        data = (self.finals if final else self.parts).get(record["id"])
        if not delivery.matches(self.info(data), record):
            raise delivery.DeliveryError("remote_integrity", unknown=True)
        if not final and self.on_part_verify is not None:
            action, self.on_part_verify = self.on_part_verify, None
            action()
        if final and self.on_final_verify is not None:
            action, self.on_final_verify = self.on_final_verify, None
            action()

    def publish(self, record):
        ident = record["id"]
        assert ident not in self.finals
        self.moves.append(ident)
        self.finals[ident] = self.parts.pop(ident)
        if self.fail_move_after_effect:
            self.fail_move_after_effect = False
            raise delivery.DeliveryError("ssh_unknown", unknown=True)

    def delete(self, record, observed, *, final):
        files = self.finals if final else self.parts
        assert self.info(files[record["id"]]) == observed
        self.deletes.append((record["id"], final))
        del files[record["id"]]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "REFRESH", "/no-such-test-refresh")
    # Production stays fixed to root; only isolated test files use the runner's UID.
    monkeypatch.setattr(delivery, "OWNER_UID", os.geteuid())
    monkeypatch.setattr(delivery.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, fd
    finally:
        os.close(fd)


def test_production_owner_is_root():
    assert delivery.OWNER_UID == 0


def test_production_refresh_path_matches_installed_package():
    assert delivery.REFRESH == "/usr/local/libexec/autostop-crm-backup/crm_backup_coordinator.py"


def test_status_refreshes_installed_coordinator(store, monkeypatch):
    expected = "/usr/local/libexec/autostop-crm-backup/crm_backup_coordinator.py"
    monkeypatch.setattr(delivery, "REFRESH", expected)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery.subprocess, "run", run)
    record = prepare(store, 1, b"synthetic")
    delivery.set_status(store[1], record, "ok")
    assert calls == [[sys.executable, expected, "refresh"]]


def prepare(store, number, contents):
    path, fd = store
    ident = f"{number:032x}"
    record = {
        "id": ident,
        "file": f"crm-{ident}.tar.gz",
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
    }
    file = path / record["file"]
    file.write_bytes(contents)
    file.chmod(0o600)
    delivery.write_json(fd, "pending.json", record)
    delivery.write_json(fd, "pending-ready.json", record)
    delivery.write_json(fd, "attempt.json", {"attempt": ident, "copy": "ok"})
    return record


def test_success_retains_only_two_known_completed_generations(store):
    remote = FakeWindows()
    records = []
    for number in range(1, 4):
        record = prepare(store, number, f"generation-{number}".encode())
        records.append(record)
        delivery.deliver_once(store[1], remote)
        assert not (store[0] / record["file"]).exists()
    assert set(remote.finals) == {records[-2]["id"], records[-1]["id"]}
    assert remote.deletes == [(records[0]["id"], True)]
    assert delivery.journal(store[1]) == records[-2:]
    assert delivery.read_json(store[1], "pending.json") is None
    assert delivery.read_json(store[1], "pending-ready.json") is None


def test_failed_write_is_retried_with_same_id_and_keeps_old_backups(store):
    remote = FakeWindows()
    old = []
    for number in (1, 2):
        record = prepare(store, number, f"old-{number}".encode())
        old.append(record)
        delivery.deliver_once(store[1], remote)
    newest = prepare(store, 3, b"new-synthetic-data")
    remote.fail_write = True
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {r["id"] for r in old}
    assert (store[0] / newest["file"]).exists()
    assert delivery.read_json(store[1], "delivery-intent.json") == newest
    delivery.deliver_once(store[1], remote)
    assert remote.writes[-2:] == [newest["id"], newest["id"]]
    assert set(remote.finals) == {old[-1]["id"], newest["id"]}


def test_unknown_move_rechecks_exact_final_without_duplicate_write(store):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    remote.fail_move_after_effect = True
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") == record
    assert (store[0] / record["file"]).exists()
    delivery.deliver_once(store[1], remote)
    assert remote.writes == [record["id"]]
    assert remote.moves == [record["id"]]
    assert not (store[0] / record["file"]).exists()


def test_new_pending_is_not_removed_when_old_delivery_finishes(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    newer = {}

    def publish_new_pending():
        newer.update(prepare(store, 2, b"newer"))
        (store[0] / first["file"]).unlink()

    remote.on_final_verify = publish_new_pending
    delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "pending.json") == newer
    assert (store[0] / newer["file"]).read_bytes() == b"newer"
    assert remote.finals[first["id"]] == b"first"


def test_old_missing_source_leaves_partial_file_and_allows_new_pointer(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    remote.parts[first["id"]] = b"incomplete"
    newer = prepare(store, 2, b"newer")
    (store[0] / first["file"]).unlink()
    with pytest.raises(delivery.DeliveryError, match="source_missing"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert remote.parts[first["id"]] == b"incomplete"
    delivery.deliver_once(store[1], remote)
    assert remote.finals[newer["id"]] == b"newer"
    assert remote.parts[first["id"]] == b"incomplete"


def test_old_missing_source_and_unreadable_pc_does_not_block_new_pointer(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    remote.parts[first["id"]] = b"unknown partial"
    remote.finals[first["id"]] = b"unknown final"
    newer = prepare(store, 2, b"newer")
    (store[0] / first["file"]).unlink()
    remote.busy = True
    with pytest.raises(delivery.DeliveryError, match="ssh_unknown"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert delivery.read_json(store[1], "pending.json") == newer
    assert remote.parts[first["id"]] == b"unknown partial"
    assert remote.finals[first["id"]] == b"unknown final"
    assert remote.deletes == []
    remote.busy = False
    delivery.deliver_once(store[1], remote)
    assert remote.finals[newer["id"]] == b"newer"
    assert remote.parts[first["id"]] == b"unknown partial"
    assert remote.finals[first["id"]] == b"unknown final"


def test_old_missing_source_without_new_pointer_keeps_intent(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    (store[0] / first["file"]).unlink()
    (store[0] / "pending.json").unlink()
    (store[0] / "pending-ready.json").unlink()
    remote.busy = True
    with pytest.raises(delivery.DeliveryError, match="ssh_unknown"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") == first
    assert remote.deletes == []


@pytest.mark.parametrize("case", ["missing", "different", "no_pointer"])
def test_unconfirmed_pending_prevents_all_remote_operations(store, case):
    remote = FakeWindows()
    record = prepare(store, 1, b"newest")
    # An old valid intent must not bypass the pending readiness barrier.
    delivery.write_json(store[1], "delivery-intent.json", record)
    if case == "missing":
        (store[0] / "pending-ready.json").unlink()
    elif case == "different":
        different = dict(record, sha256="f" * 64)
        delivery.write_json(store[1], "pending-ready.json", different)
    else:
        (store[0] / "pending.json").unlink()
    with pytest.raises(delivery.DeliveryError, match="pending_not_ready"):
        delivery.deliver_once(store[1], remote)
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert (store[0] / record["file"]).read_bytes() == b"newest"
    assert delivery.read_json(store[1], "delivery-intent.json") == record


def test_pending_loses_readiness_during_transfer_no_cleanup_or_rotation(store):
    remote = FakeWindows()
    old = []
    for number in (1, 2):
        record = prepare(store, number, f"old-{number}".encode())
        old.append(record)
        delivery.deliver_once(store[1], remote)
    newest = prepare(store, 3, b"newest")
    remote.on_part_verify = lambda: (store[0] / "pending-ready.json").unlink()
    with pytest.raises(delivery.DeliveryError, match="pending_not_ready"):
        delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {record["id"] for record in [*old, newest]}
    assert remote.finals[newest["id"]] == b"newest"
    assert remote.deletes == []
    assert (store[0] / newest["file"]).exists()
    assert remote.writes.count(newest["id"]) == 1
    assert remote.moves.count(newest["id"]) == 1
    delivery.write_json(store[1], "pending-ready.json", newest)
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {old[-1]["id"], newest["id"]}
    assert remote.writes.count(newest["id"]) == 1
    assert remote.moves.count(newest["id"]) == 1


def test_completed_delivery_recovers_stale_ready_after_pending_was_removed(store):
    remote = FakeWindows()
    record = prepare(store, 1, b"confirmed")
    delivery.write_json(store[1], "delivery-intent.json", record)
    delivery.remember(store[1], record)
    remote.finals[record["id"]] = b"confirmed"
    (store[0] / record["file"]).unlink()
    (store[0] / "pending.json").unlink()
    delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "pending-ready.json") is None
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert delivery.journal(store[1]) == [record]
    assert remote.writes == remote.moves == remote.deletes == []
    assert remote.finals[record["id"]] == b"confirmed"


def test_stale_ready_does_not_clear_without_matching_confirmed_intent(store):
    record = prepare(store, 1, b"leave-local-file")
    delivery.remember(store[1], record)
    (store[0] / "pending.json").unlink()
    with pytest.raises(delivery.DeliveryError, match="pending_not_ready"):
        delivery.pending_record(store[1])
    assert delivery.read_json(store[1], "pending-ready.json") == record
    assert (store[0] / record["file"]).read_bytes() == b"leave-local-file"


def test_stale_ready_repair_never_removes_local_archive(store):
    record = prepare(store, 1, b"leave-local-file")
    delivery.write_json(store[1], "delivery-intent.json", record)
    delivery.remember(store[1], record)
    (store[0] / "pending.json").unlink()
    assert delivery.pending_record(store[1]) is None
    assert delivery.read_json(store[1], "pending-ready.json") is None
    assert (store[0] / record["file"]).read_bytes() == b"leave-local-file"


def test_conflicting_old_final_is_preserved_when_new_generation_exists(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    remote.finals[first["id"]] = b"unexpected existing file"
    newer = prepare(store, 2, b"newer")
    with pytest.raises(delivery.DeliveryError, match="remote_conflict"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert remote.finals[first["id"]] == b"unexpected existing file"
    assert remote.deletes == []
    delivery.deliver_once(store[1], remote)
    assert remote.finals[newer["id"]] == b"newer"


def test_failed_rotation_keeps_all_old_generations_then_retries(store):
    remote = FakeWindows()
    records = []
    for number in (1, 2):
        record = prepare(store, number, f"generation-{number}".encode())
        records.append(record)
        delivery.deliver_once(store[1], remote)
    newest = prepare(store, 3, b"generation-3")
    remote.bad_inspection = records[-1]["id"]
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {r["id"] for r in [*records, newest]}
    assert remote.deletes == []
    assert delivery.read_json(store[1], "delivery-status.json")["error_code"] == "rotation_pending"
    remote.bad_inspection = None
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {records[-1]["id"], newest["id"]}
    assert delivery.read_json(store[1], "delivery-status.json")["error_code"] is None


@pytest.mark.parametrize("attempt", ["unknown", "different"])
def test_unconfirmed_attempt_never_rotates_old_generations(store, attempt):
    remote = FakeWindows()
    old = []
    for number in (1, 2):
        record = prepare(store, number, f"old-{number}".encode())
        old.append(record)
        delivery.deliver_once(store[1], remote)
    newest = prepare(store, 3, b"newest")
    result = {"attempt": newest["id"], "copy": "unknown"}
    if attempt == "different":
        result = {"attempt": "f" * 32, "copy": "ok"}
    delivery.write_json(store[1], "attempt.json", result)
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {r["id"] for r in [*old, newest]}
    assert remote.deletes == []
    assert delivery.read_json(store[1], "delivery-status.json")["error_code"] == "rotation_pending"
    delivery.write_json(store[1], "attempt.json", {"attempt": newest["id"], "copy": "ok"})
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {old[-1]["id"], newest["id"]}


def test_directory_fsync_failure_refuses_visible_markers_without_pc_access(store, monkeypatch):
    record = prepare(store, 1, b"synthetic")
    remote = FakeWindows()

    def fail_fsync(_fd):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(delivery.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        delivery.deliver_once(store[1], remote)
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert (store[0] / record["file"]).read_bytes() == b"synthetic"


def test_attempt_fsync_failure_refuses_rotation_before_pc_access(store, monkeypatch):
    remote = FakeWindows()
    records = []
    for number in (1, 2, 3):
        record = prepare(store, number, f"generation-{number}".encode())
        records.append(record)
        delivery.remember(store[1], record)
        remote.finals[record["id"]] = f"generation-{number}".encode()
    original = delivery.os.fsync
    calls = 0

    def fail_attempt_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic attempt sync failure")
        return original(fd)

    monkeypatch.setattr(delivery.os, "fsync", fail_attempt_sync)
    with pytest.raises(OSError, match="attempt sync failure"):
        delivery.rotate(store[1], remote)
    assert remote.inspections == remote.deletes == []
    assert set(remote.finals) == {record["id"] for record in records}


def test_busy_operation_lock_defers_rotation_without_waiting(store):
    record = prepare(store, 1, b"synthetic")
    guard = delivery.lock(store[1], "operation.lock")
    try:
        assert delivery.rotation_authorized(store[1], record) is False
    finally:
        os.close(guard)


def test_busy_remote_preserves_source_and_previous_generations(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.deliver_once(store[1], remote)
    newest = prepare(store, 2, b"second")
    remote.busy = True
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver_once(store[1], remote)
    assert (store[0] / newest["file"]).exists()
    assert delivery.read_json(store[1], "pending.json") == newest
    assert remote.finals[first["id"]] == b"first"
    assert remote.deletes == []


def test_refuses_symlink_source_and_marks_failure(store):
    record = prepare(store, 1, b"synthetic")
    path = store[0] / record["file"]
    path.unlink()
    path.symlink_to("unrelated")
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver_once(store[1], FakeWindows())
    assert delivery.read_json(store[1], "delivery-status.json")["status"] == "failed"
    assert path.is_symlink()


def test_stalled_remote_read_times_out_and_reaps_process(store, monkeypatch):
    record = prepare(store, 1, b"synthetic payload")
    pid_file = store[0] / "reader.pid"
    command = [
        sys.executable,
        "-c",
        "import os,sys,time; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "sys.stdout.buffer.write(b'x'); sys.stdout.buffer.flush(); time.sleep(30)",
    ]
    monkeypatch.setattr(delivery.transfer, "command", lambda *a, **kw: command)
    monkeypatch.setattr(delivery, "VERIFY_TIMEOUT_SECONDS", 0.4)
    start = time.monotonic()
    with pytest.raises(delivery.DeliveryError, match="remote_read_unknown") as error:
        delivery.Windows().verify(record, final=True)
    assert error.value.unknown
    assert time.monotonic() - start < 5
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert (store[0] / record["file"]).exists()


def test_bounded_digest_checks_all_bytes_and_size():
    command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'synthetic')"]
    assert delivery.bounded_remote_digest(command, 9, 5) == (9, hashlib.sha256(b"synthetic").digest())
    with pytest.raises(RuntimeError, match="larger"):
        delivery.bounded_remote_digest(command, 8, 5)


def test_failed_refresh_after_delivery_retries_without_another_write(store, monkeypatch):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # Start refresh, running status, then the confirmed delivery status.
        return SimpleNamespace(returncode=1 if len(calls) == 3 else 0)

    monkeypatch.setattr(delivery.subprocess, "run", run)
    with pytest.raises(delivery.DeliveryError, match="status_refresh_failed"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-status.json")["status"] == "ok"
    assert delivery.read_json(store[1], "pending.json") is None
    assert remote.writes == [record["id"]]
    assert remote.moves == [record["id"]]
    delivery.deliver_once(store[1], remote)
    assert len(calls) == 4
    assert remote.writes == [record["id"]]
    assert remote.moves == [record["id"]]


def test_failed_refresh_with_no_pending_returns_error_without_pc_access(store, monkeypatch):
    remote = FakeWindows()
    monkeypatch.setattr(delivery.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(delivery.DeliveryError, match="status_refresh_failed"):
        delivery.deliver_once(store[1], remote)
    assert remote.writes == remote.moves == remote.deletes == []
    monkeypatch.setattr(delivery, "ROOT", store[0])
    assert delivery.main() == 1


@pytest.mark.skipif(not shutil.which("pwsh"), reason="PowerShell parser not installed")
def test_powershell_inspection_and_delete_on_artificial_files(tmp_path):
    part = tmp_path / "искусственный файл.part"
    final = tmp_path / "existing.backup"
    part.write_bytes(b"synthetic\x00data")
    final.write_bytes(b"unrelated")
    preamble = (
        "$ErrorActionPreference='Stop'; "
        f"$part='{part}'; $final='{final}'; "
        "function Assert-PrivateFile([string]$path) {}\n"
    )
    inspected = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", preamble + delivery.STATUS],
        capture_output=True,
        check=True,
    )
    observed = json.loads(inspected.stdout)
    assert observed["part"] == FakeWindows.info(b"synthetic\x00data")
    assert observed["final"] == FakeWindows.info(b"unrelated")
    script = (
        delivery.DELETE.replace("__PATH__", "$part")
        .replace("__SIZE__", str(part.stat().st_size))
        .replace("__HASH__", observed["part"]["sha256"])
    )
    subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", preamble + script],
        capture_output=True,
        check=True,
    )
    assert not part.exists()
    assert final.read_bytes() == b"unrelated"
