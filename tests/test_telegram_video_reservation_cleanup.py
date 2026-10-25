"""A failed close must not leave an owned reservation blocking future previews."""

import errno
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest

from autostop_manager import telegram_video_preview as preview


class ReservationOS:
    def __init__(self, output, failure=None):
        self.output = output
        self.failure = failure
        self.reservation_fd = None
        self.reservations = self.close_calls = self.verified_closes = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        descriptor = os.open(path, flags, *args, **kwargs)
        if Path(path) == self.output and flags & os.O_CREAT and flags & os.O_EXCL:
            self.reservation_fd = descriptor
            self.reservations += 1
        return descriptor

    def close(self, descriptor):
        if descriptor != self.reservation_fd:
            os.close(descriptor)
            return
        self.reservation_fd = None
        self.close_calls += 1
        os.close(descriptor)
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
        self.verified_closes += 1
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure


@pytest.fixture
def private_preview(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("preview cleanup test attempted external activity")

    for owner, name in (
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (sqlite3, "connect"),
    ):
        monkeypatch.setattr(owner, name, denied)
    inbox = tmp_path / "owned-inbox"
    inbox.mkdir(mode=0o700)
    video = inbox / "synthetic.mp4"
    video.write_bytes(b"synthetic private video")
    video.chmod(0o600)
    output = inbox / "synthetic.preview.jpg"
    renders = []
    monkeypatch.setattr(
        preview,
        "_probe_video",
        lambda _path: {
            "codec": "h264",
            "duration_seconds": 2.5,
            "width": 640,
            "height": 360,
            "audio_present": False,
        },
    )

    def render(source, destination, **_kwargs):
        renders.append(source)
        destination.write_bytes(b"\xff\xd8\xffsynthetic preview\xff\xd9")

    monkeypatch.setattr(preview, "_render_storyboard", render)
    return inbox, video, output, renders


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_reservation_close_failure_cleans_owned_path_and_allows_recovery(private_preview, monkeypatch, error_type):
    inbox, video, output, renders = private_preview
    failure = OSError(errno.EIO, "synthetic close failure") if error_type is OSError else error_type("synthetic close")
    operations = ReservationOS(output, failure)
    monkeypatch.setattr(preview, "os", operations)
    original = video.read_bytes()

    with pytest.raises(error_type) as caught:
        preview.build_private_video_storyboard(video, inbox_dir=inbox)

    assert caught.value is failure
    assert not output.exists() and not output.is_symlink()
    assert video.read_bytes() == original and renders == []
    assert operations.reservations == operations.close_calls == operations.verified_closes == 1

    result = preview.build_private_video_storyboard(video, inbox_dir=inbox)
    assert result["ok"] and result["preview_path"] == str(output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert renders == [video]
    assert operations.reservations == operations.close_calls == operations.verified_closes == 2


@pytest.mark.parametrize("symlink", [False, True], ids=["regular", "symlink"])
def test_failed_reservation_preserves_existing_preview_and_target(private_preview, monkeypatch, symlink):
    inbox, video, output, renders = private_preview
    target = inbox / "existing.jpg" if symlink else output
    target.write_bytes(b"existing owned artifact")
    target.chmod(0o600)
    if symlink:
        output.symlink_to(target.name)
    original_inode = output.lstat().st_ino
    operations = ReservationOS(output)
    monkeypatch.setattr(preview, "os", operations)

    with pytest.raises(preview.VideoPreviewError, match="storyboard_unavailable"):
        preview.build_private_video_storyboard(video, inbox_dir=inbox)

    assert target.read_bytes() == b"existing owned artifact"
    assert output.lstat().st_ino == original_inode and output.is_symlink() is symlink
    assert operations.reservations == operations.close_calls == operations.verified_closes == 0
    assert video.exists() and renders == []


def test_cli_close_failure_keeps_error_family_and_removes_temporary_files(private_preview, monkeypatch, capsys):
    inbox, video, output, renders = private_preview
    operations = ReservationOS(output, OSError(errno.EIO, "synthetic close failure"))
    monkeypatch.setattr(preview, "os", operations)
    monkeypatch.setattr(preview, "account_inbox_path", lambda _account: inbox)

    result = preview.main(["--account", "work", "--file", str(video), "--delete-after"])

    assert result == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "video_preview_failed"}
    assert not video.exists() and not output.exists() and not output.is_symlink()
    assert operations.reservations == operations.close_calls == operations.verified_closes == 1
    assert renders == []
