"""Owned temporary JSON descriptors close even before a file context exists."""

from __future__ import annotations

import errno
import os
import stat

import pytest

from scripts.disk_maintenance import util


class FailingOS:
    def __init__(self, operation):
        self.operation = operation
        self.descriptor = None
        self.fault_seen = False
        self.error = OSError(errno.EIO, f"synthetic owned {operation} failure")

    def __getattr__(self, name):
        return getattr(os, name)

    def fchmod(self, descriptor, mode):
        self.descriptor = descriptor
        if self.operation == "fchmod":
            self.fault_seen = True
            raise self.error
        return os.fchmod(descriptor, mode)

    def fdopen(self, descriptor, *args, **kwargs):
        self.descriptor = descriptor
        self.fault_seen = True
        raise self.error


@pytest.mark.parametrize("operation", ["fchmod", "fdopen"])
def test_atomic_json_closes_owned_descriptor_before_handoff_failure(monkeypatch, tmp_path, operation):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "receipt.json"
    original = b'{"keep":"original synthetic receipt"}\n'
    target.write_bytes(original)
    target.chmod(0o600)
    failed_os = FailingOS(operation)
    monkeypatch.setattr(util, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(util, "os", failed_os)
    closed = False

    try:
        with pytest.raises(OSError) as failed:
            util.atomic_json(target, {"keep": "replacement"})

        assert failed.value is failed_os.error
        assert failed_os.fault_seen and failed_os.descriptor is not None
        assert target.read_bytes() == original
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert not list(private.glob(".maintenance-*"))
        with pytest.raises(OSError) as descriptor_state:
            os.fstat(failed_os.descriptor)
        assert descriptor_state.value.errno == errno.EBADF
        closed = True
    finally:
        # Keep a failing baseline run from leaking the descriptor into pytest.
        if not closed and failed_os.descriptor is not None:
            os.close(failed_os.descriptor)
