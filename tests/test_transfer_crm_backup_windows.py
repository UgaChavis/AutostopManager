import base64
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transfer-crm-backup-windows.py"
spec = importlib.util.spec_from_file_location("crm_windows_transfer", SCRIPT)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class SyntheticRemote:
    """Small SSH substitute; never connects to Windows."""

    def __init__(self, failure=None):
        self.files = {"existing.backup": b"old verified backup"}
        self.failure = failure
        self.published = False

    def run(self, command, stdin, **kwargs):
        script = base64.b64decode(command[-1]).decode("utf-16le")
        if "[IO.FileMode]::CreateNew" in script:
            self.files["new.part"] = stdin.read()
            return SimpleNamespace(returncode=1 if self.failure == "write" else 0)
        if "[IO.File]::Move($part, $final)" in script:
            self.files["new.backup"] = self.files.pop("new.part")
            self.published = True
            return SimpleNamespace(returncode=1 if self.failure == "unknown_publish" else 0)
        raise AssertionError("unexpected SSH command")

    def read(self, command, expected_size):
        script = base64.b64decode(command[-1]).decode("utf-16le")
        name = "new.backup" if "$path = $final" in script else "new.part"
        data = self.files[name]
        if self.failure == "mismatch" and name == "new.part":
            data = b"incorrect"
        return transfer.digest(io.BytesIO(data))


@pytest.mark.parametrize("failure", [None, "write", "mismatch", "unknown_publish"])
def test_transfer_preserves_existing_files_and_source(tmp_path, failure):
    source = tmp_path / "synthetic.backup"
    source.write_bytes(b"synthetic data\x00\xff")
    remote = SyntheticRemote(failure)
    with patch.object(transfer.subprocess, "run", remote.run), patch.object(transfer, "remote_digest", remote.read):
        if failure:
            with pytest.raises(RuntimeError):
                transfer.transfer(source)
        else:
            transfer.transfer(source)
    assert source.read_bytes() == b"synthetic data\x00\xff"
    assert remote.files["existing.backup"] == b"old verified backup"
    assert remote.published == (failure in (None, "unknown_publish"))
    if failure in ("write", "mismatch"):
        assert remote.files["new.part"] == source.read_bytes()
    else:
        assert remote.files["new.backup"] == source.read_bytes()


def test_refuses_symlink_source(tmp_path):
    source = tmp_path / "synthetic.backup"
    source.write_bytes(b"synthetic")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        transfer.transfer(link)
