"""Real temporary drop-in files with faults confined to their owned FDs."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import automation_timers
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


class DropinOS:
    """Delegate real OS operations; perturb only this attempt's temporary file."""

    def __init__(self, directory, fault):
        self.directory = directory
        self.fault = fault
        self.owned_fds = set()
        self.write_calls = 0
        self.fault_seen = False

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, mode=0o777, **kwargs):
        descriptor = os.open(path, flags, mode, **kwargs)
        candidate = Path(path)
        if candidate.parent == self.directory and candidate.name.startswith(".50-autostop-automation."):
            self.owned_fds.add(descriptor)
        return descriptor

    def close(self, descriptor):
        try:
            os.close(descriptor)
        finally:
            self.owned_fds.discard(descriptor)

    def write(self, descriptor, content):
        if descriptor in self.owned_fds:
            self.write_calls += 1
            if self.fault == "short":
                self.fault_seen = True
                return os.write(descriptor, content[:7])
            if self.fault == "zero":
                self.fault_seen = True
                if self.write_calls > 1:
                    raise AssertionError("writer retried a zero-progress syscall instead of failing closed")
                return 0
            if self.fault == "write_enospc":
                self.fault_seen = True
                raise OSError(errno.ENOSPC, "synthetic owned-file write failure")
        return os.write(descriptor, content)

    def fsync(self, descriptor):
        if descriptor in self.owned_fds and self.fault == "fsync_eio":
            self.fault_seen = True
            raise OSError(errno.EIO, "synthetic pre-publication fsync failure")
        return os.fsync(descriptor)


@pytest.mark.parametrize("operation", ["stage", "restore"])
@pytest.mark.parametrize("fault", ["normal", "short", "zero", "write_enospc", "fsync_eio"])
def test_dropin_publication_is_complete_or_preserves_previous_file(monkeypatch, tmp_path, operation, fault):
    policy = SystemTimerPolicy("synthetic", "autostop-synthetic.timer", "Synthetic timer", "managed")
    root = tmp_path / "systemd"
    target = root / (policy.unit_name + ".d") / "50-autostop-automation.conf"
    target.parent.mkdir(parents=True)
    original = b"# Original operator settings\n[Timer]\nOnUnitActiveSec=15min\nAccuracySec=2min\n"
    target.write_bytes(original)
    target.chmod(0o640)
    foreign = target.parent / ".50-autostop-automation.foreign-owner.tmp"
    foreign.write_bytes(b"another attempt owns this file")
    service_calls = []

    def forbidden_runner(command):
        service_calls.append(command)
        raise AssertionError("drop-in file operations must not run service commands")

    controller = SystemTimerController(
        policies={policy.timer_id: policy},
        runner=forbidden_runner,
        dropin_root=root,
        store=AutomationStore(tmp_path / "registry.sqlite3"),
    )
    snapshot = controller._capture_dropin(policy)
    if operation == "restore":
        target.write_bytes(b"# Current candidate\n[Timer]\nOnUnitActiveSec=30min\n")
        target.chmod(0o600)
    before = target.read_bytes()
    before_info = target.stat()
    observed_os = DropinOS(target.parent, fault)
    monkeypatch.setattr(automation_timers, "os", observed_os)

    def publish():
        if operation == "stage":
            return controller.stage_interval_dropin(
                policy.timer_id, every_minutes=20, expected_sha256=snapshot["sha256"]
            )
        controller._restore_dropin(snapshot)
        return None

    if fault in {"zero", "write_enospc", "fsync_eio"}:
        with pytest.raises((OSError, AutomationError)):
            publish()
        assert observed_os.fault_seen
        assert target.read_bytes() == before
        current = target.stat()
        assert stat.S_IMODE(current.st_mode) == stat.S_IMODE(before_info.st_mode)
        assert (current.st_uid, current.st_gid) == (before_info.st_uid, before_info.st_gid)
    else:
        receipt = publish()
        if fault == "short":
            assert observed_os.fault_seen
        expected = controller.render_interval_dropin(policy.timer_id, every_minutes=20).encode()
        if operation == "restore":
            expected = original
        actual = target.read_bytes()
        assert actual == expected
        assert hashlib.sha256(actual).hexdigest() == (receipt["sha256"] if operation == "stage" else snapshot["sha256"])
        current = target.stat()
        assert stat.S_IMODE(current.st_mode) == (0o644 if operation == "stage" else 0o640)
        assert (current.st_uid, current.st_gid) == (before_info.st_uid, before_info.st_gid)

    assert not observed_os.owned_fds, "owned file descriptor leaked"
    assert foreign.read_bytes() == b"another attempt owns this file"
    assert sorted(target.parent.glob("*.tmp")) == [foreign]
    assert not service_calls


@pytest.mark.parametrize("operation", ["stage", "restore"])
def test_exclusive_temp_collision_preserves_foreign_file_and_target(monkeypatch, tmp_path, operation):
    policy = SystemTimerPolicy("synthetic", "autostop-synthetic.timer", "Synthetic timer", "managed")
    root = tmp_path / "systemd"
    target = root / (policy.unit_name + ".d") / "50-autostop-automation.conf"
    target.parent.mkdir(parents=True)
    original = b"# Original operator settings\n[Timer]\nOnUnitActiveSec=15min\n"
    target.write_bytes(original)
    target.chmod(0o640)
    service_calls = []

    def forbidden_runner(command):
        service_calls.append(command)
        raise AssertionError("drop-in file operations must not run service commands")

    controller = SystemTimerController(
        policies={policy.timer_id: policy},
        runner=forbidden_runner,
        dropin_root=root,
        store=AutomationStore(tmp_path / "registry.sqlite3"),
    )
    snapshot = controller._capture_dropin(policy)
    token = "a" * 32
    monkeypatch.setattr(automation_timers, "uuid4", lambda: SimpleNamespace(hex=token))
    prefix = ".50-autostop-automation." if operation == "stage" else ".50-autostop-automation.rollback."
    collision = target.parent / (prefix + token + ".tmp")
    collision.write_bytes(b"another attempt owns this exact path")

    with pytest.raises(FileExistsError):
        if operation == "stage":
            controller.stage_interval_dropin(policy.timer_id, every_minutes=20, expected_sha256=snapshot["sha256"])
        else:
            controller._restore_dropin(snapshot)

    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert collision.read_bytes() == b"another attempt owns this exact path"
    assert sorted(target.parent.glob("*.tmp")) == [collision]
    assert not service_calls
