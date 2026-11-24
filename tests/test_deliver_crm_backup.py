import base64
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
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
        self.flushes = []
        self.moves = []
        self.deletes = []
        self.fail_write = False
        self.fail_flush = False
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
        self.flush_part(record)

    def flush_part(self, record):
        ident = record["id"]
        assert len(self.parts[ident]) == record["size"]
        self.flushes.append(ident)
        if self.fail_flush:
            raise delivery.DeliveryError("ssh_unknown", unknown=True, transport_diagnostic=("flush", "ssh_exit_255"))

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


@pytest.fixture
def delivery_clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 1, 1, 3, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(delivery, "dt", SimpleNamespace(UTC=UTC, datetime=Clock))
    return Clock


def test_production_owner_is_root():
    assert delivery.OWNER_UID == 0


def test_production_refresh_path_matches_installed_package():
    assert delivery.REFRESH == "/usr/local/libexec/autostop-crm-backup/crm_backup_coordinator.py"


def test_native_sftp_upload_uses_private_empty_create_and_open_generation(tmp_path, monkeypatch):
    data = b"synthetic\x00" * (3 * 1024 * 1024 // 10 + 1)
    ident = "a" * 32
    record = {"id": ident, "file": f"crm-{ident}.tar.gz", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    path = tmp_path / record["file"]
    path.write_bytes(data)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ssh":
            script = base64.b64decode(command[-1]).decode("utf-16le")
            assert "OpenStandardInput" not in script and "$stream.Write" not in script
            assert kwargs["stdin"] == subprocess.DEVNULL
            if len(calls) == 1:
                assert "FileMode]::CreateNew" in script
                assert "SetAccessRuleProtection($true, $false)" in script
            else:
                assert len(calls) == 3 and calls[-2][0][0] == "sftp"
                assert "Assert-PrivateFile $part" in script
                assert "FileAccess]::ReadWrite, [IO.FileShare]::None" in script
                assert f"$stream.Length -ne {len(data)}" in script
                assert "$stream.Flush($true)" in script
        else:
            assert command[0] == "sftp"
            assert command[command.index("-s") + 1] == "C:/Windows/System32/OpenSSH/sftp-server.exe"
            assert command[5:-1] == delivery.transfer.SSH[2:-1]
            assert command[-1] == delivery.transfer.HOST
            batch = kwargs["input"]
            assert len(batch) < 1024 and "stdin" not in kwargs
            operation, local, remote = shlex.split(batch.decode("utf-8"))
            assert operation == "put"
            assert local == f"/proc/{os.getpid()}/fd/{source.fileno()}"
            assert Path(local).read_bytes() == data
            assert remote == f"/C:/Users/9860606/Desktop/Резерв CRM/crm-{ident}.part"
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(delivery.subprocess, "run", run)
    with path.open("rb") as source:
        # The caller's pinned source remains readable after its name is retired.
        path.unlink()
        delivery.Windows().write(record, source)
    assert [command[0] for command, _ in calls] == ["ssh", "sftp", "ssh"]


@pytest.mark.parametrize("failed_stage", ["create", "sftp", "flush"])
def test_native_upload_failure_is_unknown_and_never_publishes(tmp_path, monkeypatch, failed_stage):
    data = b"synthetic"
    ident = "b" * 32
    record = {"id": ident, "file": f"crm-{ident}.tar.gz", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    path = tmp_path / record["file"]
    path.write_bytes(data)
    calls = []

    def run(command, **_kwargs):
        calls.append(command[0])
        stage = {1: "create", 2: "sftp", 3: "flush"}[len(calls)]
        return SimpleNamespace(returncode=1 if stage == failed_stage else 0, stdout=b"")

    monkeypatch.setattr(delivery.subprocess, "run", run)
    with path.open("rb") as source, pytest.raises(delivery.DeliveryError, match="ssh_unknown") as error:
        delivery.Windows().write(record, source)
    assert error.value.unknown
    expected_diagnostic = ("upload", "sftp_nonzero") if failed_stage == "sftp" else (failed_stage, "remote_nonzero")
    assert error.value.transport_diagnostic == expected_diagnostic
    assert calls == {"create": ["ssh"], "sftp": ["ssh", "sftp"], "flush": ["ssh", "sftp", "ssh"]}[failed_stage]
    assert path.read_bytes() == data


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


def test_ssh_inspect_preserves_success_stdout_contract(store, monkeypatch, capsys):
    record = prepare(store, 1, b"synthetic")
    metadata = {"part": None, "final": {"size": record["size"], "sha256": record["sha256"]}}

    def run(command, **options):
        assert command[0] == "ssh"
        assert options == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "timeout": 21600,
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(metadata).encode())

    monkeypatch.setattr(delivery.subprocess, "run", run)
    assert delivery.Windows().inspect(record) == metadata
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("failure", "reason"),
    [("timeout", "timeout"), ("os_error", "os_error"), (-15, "signal"), (255, "ssh_exit_255"), (7, "remote_nonzero")],
)
def test_transport_diagnostics_preserve_receipt_retry_and_scrub_stderr(store, monkeypatch, capsys, failure, reason):
    root, fd = store
    record = prepare(store, 1, b"synthetic")
    private_command = ["ssh", "test-private-address", "test-private-arg"]
    private_stdout = b"test-private-stdout"
    private_stderr = b"test-private-stderr"
    calls = []

    def run(command, **options):
        assert command[0] == "ssh"
        assert options["stdout"] == subprocess.PIPE
        assert options["stderr"] == subprocess.DEVNULL
        assert options["timeout"] == 21600
        calls.append(command[0])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(private_command, 21600, output=private_stdout, stderr=private_stderr)
        if failure == "os_error":
            raise OSError("test-private-os-error")
        return subprocess.CompletedProcess(private_command, failure, stdout=private_stdout, stderr=private_stderr)

    monkeypatch.setattr(delivery, "ROOT", root)
    monkeypatch.setattr(delivery, "refresh_status", lambda: None)
    monkeypatch.setattr(delivery.subprocess, "run", run)
    assert delivery.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"delivery incomplete: ssh_unknown\ndelivery transport failure: stage=inspect reason={reason}\n"
    )
    status = delivery.read_json(fd, "delivery-status.json")
    assert set(status) == {"id", "status", "at", "error_code"}
    assert (status["id"], status["status"], status["error_code"]) == (record["id"], "unknown", "ssh_unknown")
    assert delivery.read_json(fd, "pending.json") == record
    assert delivery.read_json(fd, "pending-ready.json") == record
    assert delivery.read_json(fd, "delivery-intent.json") == record
    assert (root / record["file"]).read_bytes() == b"synthetic"
    assert calls == ["ssh"]


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


def test_matching_partial_file_retries_flush_before_publishing_without_reupload(store):
    root, fd = store
    payload = b"synthetic"
    record = prepare(store, 1, payload)
    ident = record["id"]
    source = root / record["file"]
    remote = FakeWindows()
    remote.fail_flush = True

    for attempt in range(1, 3):
        with pytest.raises(delivery.DeliveryError, match="ssh_unknown") as failure:
            delivery.deliver_once(fd, remote)
        assert failure.value.transport_diagnostic == ("flush", "ssh_exit_255")
        assert remote.parts[ident] == payload
        assert remote.writes == [ident]
        assert remote.flushes == [ident] * attempt
        assert remote.moves == []
        assert remote.finals == {}
        assert source.read_bytes() == payload
        assert delivery.read_json(fd, "pending.json") == record
        assert delivery.read_json(fd, "pending-ready.json") == record
        assert delivery.read_json(fd, "delivery-intent.json") == record
        status = delivery.read_json(fd, "delivery-status.json")
        assert (status["status"], status["error_code"]) == ("unknown", "ssh_unknown")
        assert delivery.journal(fd) == []

    remote.fail_flush = False
    delivery.deliver_once(fd, remote)
    assert remote.writes == [ident]
    assert remote.flushes == [ident] * 3
    assert remote.moves == [ident]
    assert remote.finals == {ident: payload}
    assert delivery.journal(fd) == [record]
    assert delivery.read_json(fd, "delivery-status.json")["status"] == "ok"
    assert not source.exists()


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
    assert remote.flushes == [record["id"]]
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


@pytest.mark.parametrize("contents", [b"short", b"synthetic archive followed by extra bytes"])
def test_wrong_local_archive_length_is_rejected_before_reading(store, monkeypatch, contents):
    root, fd = store
    record = prepare(store, 1, b"synthetic archive")
    (root / record["file"]).write_bytes(contents)
    remote = FakeWindows()

    def unexpected_digest(_stream):
        pytest.fail("archive bytes were read despite a known size mismatch")

    monkeypatch.setattr(delivery.transfer, "digest", unexpected_digest)
    with pytest.raises(delivery.DeliveryError, match="source_integrity"):
        delivery.deliver_once(fd, remote)
    status = delivery.read_json(fd, "delivery-status.json")
    assert (status["id"], status["status"], status["error_code"]) == (record["id"], "failed", "source_integrity")
    assert delivery.read_json(fd, "delivery-intent.json") == delivery.read_json(fd, "pending.json") == record
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []


def test_damaged_retired_source_preserves_old_files_and_allows_new_pointer(store):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    old_path = store[0] / first["file"]
    # A same-length corruption still has to fail the full checksum validation.
    old_path.write_bytes(b"other")
    remote.parts[first["id"]] = b"unknown partial"
    remote.finals[first["id"]] = b"unknown old final"
    newer = prepare(store, 2, b"newer")

    with pytest.raises(delivery.DeliveryError, match="source_integrity"):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert delivery.read_json(store[1], "pending.json") == newer
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []

    delivery.deliver_once(store[1], remote)
    assert remote.finals[newer["id"]] == b"newer"
    assert delivery.read_json(store[1], "pending.json") is None
    assert delivery.journal(store[1]) == [newer]
    assert old_path.read_bytes() == b"other"
    assert remote.parts[first["id"]] == b"unknown partial"
    assert remote.finals[first["id"]] == b"unknown old final"
    assert remote.deletes == []


@pytest.mark.parametrize("replacement", ["same_generation", "not_ready"])
def test_damaged_source_keeps_intent_without_new_ready_generation(store, replacement):
    remote = FakeWindows()
    first = prepare(store, 1, b"first")
    delivery.write_json(store[1], "delivery-intent.json", first)
    old_path = store[0] / first["file"]
    old_path.write_bytes(b"damaged old source")
    expected = "source_integrity"
    if replacement == "not_ready":
        prepare(store, 2, b"newer")
        (store[0] / "pending-ready.json").unlink()
        expected = "pending_not_ready"
    with pytest.raises(delivery.DeliveryError, match=expected):
        delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-intent.json") == first
    assert old_path.read_bytes() == b"damaged old source"
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []


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


@pytest.fixture
def interrupted_publication(store, monkeypatch):
    root, fd = store
    previous = prepare(store, 1, b"previous ready archive")
    path = SCRIPT.with_name("crm_backup_coordinator.py")
    spec = importlib.util.spec_from_file_location("crm_backup_publication_fixture", path)
    coordinator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(coordinator)
    monkeypatch.setattr(coordinator, "ROOT_UID", os.geteuid())
    save = coordinator.save

    def fail_readiness(path, record):
        if path.name == "pending-ready.json":
            raise OSError("synthetic readiness write failure")
        save(path, record)

    staged = root / ".staged-fixture.tar.gz"
    staged.write_bytes(b"new capture")
    staged.chmod(0o600)
    with monkeypatch.context() as fault:
        fault.setattr(coordinator, "save", fail_readiness)
        with pytest.raises(OSError, match="readiness write failure"):
            coordinator.publish(root, staged, f"{2:032x}")
    pending = delivery.read_json(fd, "pending.json")
    delivery.write_json(
        fd, "attempt.json", {"attempt": pending["id"], "phase": "complete", "restore": "ok", "copy": "unknown"}
    )
    assert delivery.read_json(fd, "pending-ready.json") == previous
    remote = FakeWindows()
    monkeypatch.setattr(delivery, "ROOT", root)
    monkeypatch.setattr(delivery, "Windows", lambda: remote)
    return previous, pending, remote


def test_partial_publication_recovers_previous_pointer_for_next_tick(store, interrupted_publication, capsys):
    root, fd = store
    previous, pending, remote = interrupted_publication
    for ident in ("a" * 32, "b" * 32):
        earlier = dict(previous, id=ident, file=f"crm-{ident}.tar.gz")
        delivery.remember(fd, earlier)
        remote.finals[ident] = b"previous ready archive"
    delivery.set_status(fd, earlier, "ok")
    receipts = (root / "delivered.json").read_bytes()
    status = (root / "delivery-status.json").read_bytes()
    attempt = (root / "attempt.json").read_bytes()
    ready = (root / "pending-ready.json").read_bytes()

    assert delivery.main() == 0

    assert capsys.readouterr() == ("crm_pending_repaired=previous_ready\n", "")
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert delivery.pending_record(fd) == previous
    assert (root / "pending-ready.json").read_bytes() == ready
    assert (root / "attempt.json").read_bytes() == attempt
    assert (root / previous["file"]).read_bytes() == b"previous ready archive"
    assert (root / pending["file"]).read_bytes() == b"new capture"
    assert delivery.read_json(fd, "delivery-intent.json") is None
    assert (root / "delivered.json").read_bytes() == receipts
    assert (root / "delivery-status.json").read_bytes() == status

    assert delivery.main() == 0
    assert set(remote.finals) == {"a" * 32, "b" * 32, previous["id"]}
    assert remote.finals[previous["id"]] == b"previous ready archive"
    assert remote.deletes == []
    assert (root / pending["file"]).read_bytes() == b"new capture"
    assert (root / "attempt.json").read_bytes() == attempt


@pytest.mark.parametrize(
    "blocker",
    [
        "no-ready",
        "wrong-attempt",
        "not-complete",
        "restore-unknown",
        "copy-running",
        "intent",
        "null-intent",
        "old-receipt",
        "new-receipt",
        "invalid-receipt",
        "status",
        "invalid-status-time",
        "invalid-status-code",
        "wrong-hash",
        "old-inode-missing",
        "new-inode-missing",
    ],
)
def test_partial_publication_preserves_ambiguous_state(store, interrupted_publication, blocker):
    root, fd = store
    previous, pending, remote = interrupted_publication
    if blocker == "no-ready":
        (root / "pending-ready.json").unlink()
    elif blocker in {"wrong-attempt", "not-complete", "restore-unknown", "copy-running"}:
        changes = {
            "wrong-attempt": {"attempt": "f" * 32},
            "not-complete": {"phase": "recovering"},
            "restore-unknown": {"restore": "unknown"},
            "copy-running": {"copy": "running"},
        }
        attempt = delivery.read_json(fd, "attempt.json")
        delivery.write_json(fd, "attempt.json", attempt | changes[blocker])
    elif blocker in {"intent", "null-intent"}:
        delivery.write_json(fd, "delivery-intent.json", previous if blocker == "intent" else None)
    elif blocker in {"old-receipt", "new-receipt"}:
        delivery.remember(fd, previous if blocker == "old-receipt" else pending)
    elif blocker == "invalid-receipt":
        delivery.write_json(fd, "delivered.json", [{"id": "f" * 32}])
    elif blocker == "status":
        delivery.set_status(fd, previous, "unknown")
    elif blocker in {"invalid-status-time", "invalid-status-code"}:
        status = {"id": "f" * 32, "status": "ok", "at": "2026-01-01T00:00:00+00:00", "error_code": None}
        status["at" if blocker == "invalid-status-time" else "error_code"] = 123
        delivery.write_json(fd, "delivery-status.json", status)
    elif blocker == "wrong-hash":
        (root / previous["file"]).write_bytes(b"X" * previous["size"])
    elif blocker == "old-inode-missing":
        (root / previous["file"]).unlink()
    else:
        (root / pending["file"]).unlink()
    before = {p.name: p.read_bytes() for p in root.iterdir() if not p.name.endswith(".lock")}

    assert delivery.main() == 1

    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert {p.name: p.read_bytes() for p in root.iterdir() if not p.name.endswith(".lock")} == before


@pytest.mark.parametrize("name", ["coordinator.lock", "operation.lock"])
def test_partial_publication_defers_to_native_lock_owner(store, interrupted_publication, name):
    _, fd = store
    _, pending, remote = interrupted_publication
    guard = delivery.lock(fd, name)
    try:
        assert delivery.main() == 1
    finally:
        os.close(guard)
    assert delivery.read_json(fd, "pending.json") == pending
    assert remote.inspections == []


@pytest.mark.parametrize("replace_newer", [False, True])
def test_partial_publication_rechecks_named_inodes_after_hash(
    store, interrupted_publication, monkeypatch, replace_newer
):
    root, fd = store
    previous, pending, remote = interrupted_publication
    verify = delivery.verify_source

    def replace_after_hash(stream, record):
        result = verify(stream, record)
        path = root / (pending if replace_newer else previous)["file"]
        replacement = root / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        return result

    monkeypatch.setattr(delivery, "verify_source", replace_after_hash)
    assert delivery.main() == 1
    assert delivery.read_json(fd, "pending.json") == pending
    assert delivery.read_json(fd, "pending-ready.json") == previous
    assert remote.inspections == []


@pytest.mark.parametrize("visible", [False, True])
def test_partial_publication_defers_transport_if_repair_durability_is_unknown(
    store, interrupted_publication, monkeypatch, visible
):
    root, fd = store
    previous, pending, remote = interrupted_publication
    replace, fsync = os.replace, os.fsync
    replaced = False

    def fail_before_replace(source, destination, **kwargs):
        nonlocal replaced
        if destination == "pending.json" and not visible:
            raise OSError("synthetic pre-replace failure")
        replace(source, destination, **kwargs)
        replaced = destination == "pending.json"

    def fail_after_replace(descriptor):
        if replaced:
            raise OSError("synthetic directory fsync failure")
        fsync(descriptor)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_before_replace)
        fault.setattr(os, "fsync", fail_after_replace)
        assert delivery.main() == 1

    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert delivery.read_json(fd, "pending.json") == (previous if visible else pending)
    assert delivery.read_json(fd, "pending-ready.json") == previous
    assert (root / previous["file"]).is_file() and (root / pending["file"]).is_file()
    assert delivery.read_json(fd, "delivery-intent.json") is None
    if not visible:
        assert delivery.main() == 0
        assert remote.inspections == []
    assert delivery.pending_record(fd) == previous


@pytest.mark.parametrize("marker", ["pending.json", "pending-ready.json", "delivery-intent.json"])
def test_present_null_marker_refuses_rotation(store, marker):
    root, fd = store
    remote = FakeWindows()
    records = []
    for number in (1, 2, 3):
        payload = f"generation-{number}".encode()
        record = prepare(store, number, payload)
        records.append(record)
        delivery.remember(fd, record)
        remote.finals[record["id"]] = payload
    for name in ("pending.json", "pending-ready.json"):
        delivery.unlink_file(fd, name)
    delivery.set_status(fd, records[-1], "ok", "rotation_pending")
    delivery.write_json(fd, marker, None)

    with pytest.raises(delivery.DeliveryError, match=r"^invalid_metadata$"):
        delivery.deliver_once(fd, remote)
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    assert delivery.journal(fd) == records
    assert delivery.read_json(fd, "delivery-status.json")["error_code"] == "rotation_pending"
    assert (root / marker).read_bytes() == b"null\n"

    delivery.unlink_file(fd, marker)
    delivery.deliver_once(fd, remote)
    assert remote.deletes == [(records[0]["id"], True)]
    assert delivery.journal(fd) == records[1:]
    assert delivery.read_json(fd, "delivery-status.json")["error_code"] is None


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


def test_failed_rotation_keeps_all_old_generations_then_retries(store, monkeypatch, delivery_clock):
    remote = FakeWindows()
    records = []
    for number in (1, 2):
        record = prepare(store, number, f"generation-{number}".encode())
        records.append(record)
        delivery.deliver_once(store[1], remote)
    newest = prepare(store, 3, b"generation-3")
    verified_at = delivery_clock.current.isoformat()
    remote.bad_inspection = records[-1]["id"]
    inspect = remote.inspect

    def inspect_after_verification(record):
        if record == records[-1]:
            delivery_clock.current = datetime(2026, 1, 1, 3, 5, tzinfo=UTC)
        return inspect(record)

    with monkeypatch.context() as patch:
        patch.setattr(remote, "inspect", inspect_after_verification)
        delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {r["id"] for r in [*records, newest]}
    assert remote.deletes == []
    prior = delivery.read_json(store[1], "delivery-status.json")
    assert prior["error_code"] == "rotation_pending"
    assert prior["at"] == verified_at

    remote.bad_inspection = None
    delivery_clock.current = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    delivery.deliver_once(store[1], remote)
    assert set(remote.finals) == {records[-1]["id"], newest["id"]}
    assert delivery.read_json(store[1], "delivery-status.json") == {**prior, "error_code": None}
    assert remote.writes == remote.moves == [row["id"] for row in [*records, newest]]

    newer = prepare(store, 4, b"generation-4")
    delivery.deliver_once(store[1], remote)
    status = delivery.read_json(store[1], "delivery-status.json")
    assert status["id"] == newer["id"]
    assert status["at"] == delivery_clock.current.isoformat()


@pytest.mark.parametrize("legacy_at", [None, "not-a-timestamp"])
def test_idle_note_clear_without_a_verification_time_keeps_time_unknown(store, delivery_clock, legacy_at):
    _, fd = store
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    delivery.deliver_once(fd, remote)
    prior = delivery.read_json(fd, "delivery-status.json")
    if legacy_at is None:
        prior.pop("at")
    else:
        prior["at"] = legacy_at
    delivery.write_json(fd, "delivery-status.json", {**prior, "error_code": "rotation_pending"})
    remote.inspections.clear()

    delivery_clock.current = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    delivery.deliver_once(fd, remote)

    assert remote.inspections == []
    assert delivery.journal(fd) == [record]
    assert delivery.read_json(fd, "delivery-status.json") == {
        "id": record["id"],
        "status": "ok",
        "at": "неизвестно",
        "error_code": None,
    }


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


def test_failed_m2_refresh_after_delivery_retries_without_another_write(store, monkeypatch, capsys):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # Start refresh, running status, then the confirmed delivery status.
        return SimpleNamespace(returncode=1 if len(calls) == 3 else 0)

    monkeypatch.setattr(delivery.subprocess, "run", run)
    delivery.deliver_once(store[1], remote)
    assert capsys.readouterr().err == "CRM backup: M2 status unavailable during delivery.\n"
    assert delivery.read_json(store[1], "delivery-status.json")["status"] == "ok"
    assert delivery.read_json(store[1], "pending.json") is None
    assert remote.writes == [record["id"]]
    assert remote.moves == [record["id"]]
    delivery.deliver_once(store[1], remote)
    assert len(calls) == 4
    assert remote.writes == [record["id"]]
    assert remote.moves == [record["id"]]


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_failed_m2_refresh_with_no_pending_is_nonfatal_without_pc_access(store, monkeypatch, failure):
    remote = FakeWindows()

    def run(*_args, **_kwargs):
        if failure == "missing":
            raise FileNotFoundError("synthetic missing M2 renderer")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["synthetic M2 renderer"], 20)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(delivery.subprocess, "run", run)
    delivery.deliver_once(store[1], remote)
    assert remote.inspections == remote.writes == remote.moves == remote.deletes == []
    monkeypatch.setattr(delivery, "ROOT", store[0])
    assert delivery.main() == 0


@pytest.mark.parametrize("metadata", ["delivery-status.json", "delivered.json"])
def test_failed_delivery_metadata_write_preserves_source_until_confirmed_retry(store, monkeypatch, metadata):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    original = delivery.write_json

    def denied(dirfd, name, value):
        if name == metadata:
            raise OSError("synthetic delivery metadata write failure")
        return original(dirfd, name, value)

    monkeypatch.setattr(delivery, "write_json", denied)
    with pytest.raises((delivery.DeliveryError, OSError)):
        delivery.deliver_once(store[1], remote)
    assert (store[0] / record["file"]).read_bytes() == b"synthetic"
    assert delivery.read_json(store[1], "pending.json") == record
    assert delivery.read_json(store[1], "delivery-intent.json") == record
    assert remote.deletes == []

    monkeypatch.setattr(delivery, "write_json", original)
    delivery.deliver_once(store[1], remote)
    assert remote.finals[record["id"]] == b"synthetic"
    assert delivery.read_json(store[1], "pending.json") is None
    assert delivery.read_json(store[1], "delivery-status.json")["status"] == "ok"
    assert remote.writes == [record["id"]]


def test_failed_confirmed_receipt_keeps_source_until_exact_remote_retry(store, monkeypatch):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    original = delivery.write_json

    def denied(dirfd, name, value):
        if name == "delivery-status.json" and value["status"] == "ok":
            raise OSError("synthetic confirmed receipt failure")
        return original(dirfd, name, value)

    monkeypatch.setattr(delivery, "write_json", denied)
    with pytest.raises(OSError, match="confirmed receipt failure"):
        delivery.deliver_once(store[1], remote)
    assert remote.finals[record["id"]] == b"synthetic"
    assert delivery.journal(store[1]) == [record]
    assert (store[0] / record["file"]).read_bytes() == b"synthetic"
    assert delivery.read_json(store[1], "pending.json") == record
    assert delivery.read_json(store[1], "delivery-intent.json") == record

    monkeypatch.setattr(delivery, "write_json", original)
    delivery.deliver_once(store[1], remote)
    assert delivery.read_json(store[1], "delivery-status.json")["status"] == "ok"
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert not (store[0] / record["file"]).exists()
    assert remote.writes == [record["id"]]


@pytest.mark.parametrize("saved_time_valid", [True, False])
def test_cleanup_failure_preserves_confirmed_status_and_retries_without_upload(
    store, monkeypatch, delivery_clock, saved_time_valid
):
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")
    original = delivery.cleanup_pending

    def failed_after_cleanup(*args):
        original(*args)
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(delivery, "cleanup_pending", failed_after_cleanup)
    with pytest.raises(delivery.DeliveryError, match="local_io"):
        delivery.deliver_once(store[1], remote)
    status = delivery.read_json(store[1], "delivery-status.json")
    assert status["status"] == "ok" and status["error_code"] == "cleanup_pending"
    verified_at = delivery_clock.current.isoformat()
    assert status["at"] == verified_at
    assert delivery.read_json(store[1], "delivery-intent.json") == record
    assert delivery.read_json(store[1], "pending.json") is None
    assert not (store[0] / record["file"]).exists()

    if not saved_time_valid:
        delivery.write_json(store[1], "delivery-status.json", {**status, "at": "invalid"})
    delivery_clock.current = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(delivery, "cleanup_pending", original)
    delivery.deliver_once(store[1], remote)
    status = delivery.read_json(store[1], "delivery-status.json")
    assert status["status"] == "ok" and status["error_code"] is None
    assert status["at"] == (verified_at if saved_time_valid else delivery_clock.current.isoformat())
    assert delivery.read_json(store[1], "delivery-intent.json") is None
    assert remote.writes == [record["id"]]
    assert remote.finals[record["id"]] == b"synthetic"


@pytest.mark.parametrize(
    ("observation", "failure"),
    [("offline", "ssh_unknown"), ("no_receipt", "ssh_unknown"), ("conflict", "remote_conflict")],
)
def test_cleanup_retry_keeps_exact_confirmation_only_when_remote_is_unreachable(
    store, monkeypatch, observation, failure
):
    root, fd = store
    remote = FakeWindows()
    record = prepare(store, 1, b"synthetic")

    def unavailable_cleanup(*args):
        raise OSError("synthetic cleanup stopped before unlink")

    with monkeypatch.context() as patch:
        patch.setattr(delivery, "cleanup_pending", unavailable_cleanup)
        with pytest.raises(delivery.DeliveryError, match="local_io"):
            delivery.deliver_once(fd, remote)
    prior = delivery.read_json(fd, "delivery-status.json")
    assert (prior["status"], prior["error_code"]) == ("ok", "cleanup_pending")
    assert delivery.journal(fd) == [record]

    if observation == "no_receipt":
        delivery.write_json(fd, "delivered.json", [])
    remote.busy = observation != "conflict"
    remote.bad_inspection = record["id"] if observation == "conflict" else None
    with pytest.raises(delivery.DeliveryError, match=failure):
        delivery.deliver_once(fd, remote)

    observed = delivery.read_json(fd, "delivery-status.json")
    if observation == "offline":
        assert observed == prior
    else:
        assert (observed["status"], observed["error_code"]) == ("unknown", failure)
    assert delivery.read_json(fd, "delivery-intent.json") == delivery.read_json(fd, "pending.json") == record
    assert (root / record["file"]).read_bytes() == b"synthetic"
    assert remote.deletes == []

    remote.busy = False
    remote.bad_inspection = None
    delivery.deliver_once(fd, remote)

    assert delivery.journal(fd) == [record]
    assert delivery.read_json(fd, "delivery-status.json")["error_code"] is None
    assert delivery.read_json(fd, "delivery-intent.json") is None
    assert not (root / record["file"]).exists()
    assert remote.writes == remote.moves == [record["id"]]


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
