from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import errno
import json
import os
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_monitor_voice as voice
from autostop_manager import telegram_owner as owner


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.skipif(os.geteuid() != 0, reason="real root-owned owner configuration requires root")
def test_owner_short_writes_publish_complete_binding(tmp_path, monkeypatch, replace):
    path = tmp_path / "owner.json"
    if replace:
        owner.configure_owner_peer_id(path, 41, reader_gid=os.getgid())
    real_write = os.write
    calls = []

    def partial(fd, content):
        calls.append(len(content))
        return real_write(fd, content[:3])

    monkeypatch.setattr(owner.os, "write", partial)
    owner.configure_owner_peer_id(path, 42, reader_gid=os.getgid(), replace=replace)
    assert owner.load_owner_peer_id(path) == 42
    assert len(calls) > 1 and calls[-1] <= 3
    assert path.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["zero", "error"])
@pytest.mark.skipif(os.geteuid() != 0, reason="real root-owned owner configuration requires root")
def test_owner_partial_failure_preserves_binding_and_closes_fd(tmp_path, monkeypatch, failure):
    path = tmp_path / "owner.json"
    owner.configure_owner_peer_id(path, 41, reader_gid=os.getgid())
    before = path.read_bytes()
    real_write = os.write
    descriptors = []

    def partial(fd, content):
        descriptors.append(fd)
        if len(descriptors) == 1:
            return real_write(fd, content[:3])
        if failure == "zero":
            return 0
        raise OSError(errno.ENOSPC, "synthetic full disk")

    monkeypatch.setattr(owner.os, "write", partial)
    with pytest.raises(owner.OwnerConfigError, match="owner_config_write_failed"):
        owner.configure_owner_peer_id(path, 42, reader_gid=os.getgid(), replace=True)
    assert path.read_bytes() == before and owner.load_owner_peer_id(path) == 41
    assert path.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def _stage(tmp_path):
    config = bridge.TelegramConfig(
        123456,
        "0" * 32,
        session_path=tmp_path / "state" / "account",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "bridge.sock",
    )
    monitor = bridge.InboundMonitor()
    event_id = monitor.record(SimpleNamespace(id=100, chat_id=42, is_private=True, out=False, date=datetime.now(UTC)))
    event = monitor.require_open(event_id)
    monitor.reserve_voice_stage(event)
    handle = "m" * 32
    path = bridge._save_monitor_voice_stage(
        b"OggSsynthetic", handle=handle, suffix=".ogg", inbox_dir=config.socket_path.parent / "inbox"
    )
    monitor.stage_voice(event, handle, path, duration_seconds=8, suffix=".ogg")
    return config, monitor, event_id, handle, path


def test_voice_expiry_releases_stage_already_deleted_by_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_CLEANUP_RETRY_SECONDS", 0)
    config, monitor, event_id, handle, path = _stage(tmp_path)
    path.unlink()
    asyncio.run(bridge._expire_monitor_voice_stage(config, monitor, handle))
    assert monitor.voice_stage(handle) is None
    monitor.reserve_voice_stage(monitor.require_open(event_id))


def test_event_voice_recovery_is_idempotent_and_hides_handle(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    config, monitor, event_id, handle, path = _stage(tmp_path)
    path.unlink()
    first = asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    second = asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert first["cleanup_verified"] and first["recovered"] and not first["stage_active"]
    assert second["cleanup_verified"] and not second["stage_active"]
    assert handle not in json.dumps(first) and str(path) not in json.dumps(first)
    assert monitor.voice_stage(handle) is None


def test_event_voice_recovery_does_not_delete_fresh_runner_file(tmp_path):
    config, monitor, event_id, handle, path = _stage(tmp_path)
    with pytest.raises(bridge.BridgeError, match="inbound_voice_stage_active"):
        asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert path.exists() and monitor.voice_stage(handle) is not None


def test_event_voice_recovery_accepts_missing_file_before_ttl(tmp_path):
    config, monitor, event_id, handle, path = _stage(tmp_path)
    path.unlink()
    result = asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert result["cleanup_verified"] and monitor.voice_stage(handle) is None


def test_event_voice_recovery_preserves_replaced_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    config, monitor, event_id, handle, path = _stage(tmp_path)
    other = path.with_suffix(".new")
    other.write_bytes(b"OggSreplacement")
    other.chmod(0o600)
    os.replace(other, path)
    with pytest.raises(bridge.BridgeError, match="download_permissions_invalid"):
        asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert path.read_bytes() == b"OggSreplacement" and monitor.voice_stage(handle) is not None


def test_event_voice_cleanup_permission_error_is_not_absence(tmp_path, monkeypatch):
    config, monitor, event_id, handle, path = _stage(tmp_path)
    original = os.stat

    def fail(name, *args, **kwargs):
        if name == path.name and kwargs.get("dir_fd") is not None:
            raise PermissionError(errno.EACCES, "synthetic denied")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(bridge.os, "stat", fail)
        with pytest.raises(PermissionError):
            asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert path.exists() and monitor.voice_stage(handle) is not None


def test_event_voice_cleanup_rejects_replaced_directory_even_when_path_is_missing(tmp_path):
    config, monitor, event_id, handle, path = _stage(tmp_path)
    original = path.parent.with_name("original-inbox")
    path.parent.rename(original)
    path.parent.mkdir(mode=0o700)
    with pytest.raises(bridge.BridgeError, match="download_inbox_permissions_invalid"):
        asyncio.run(bridge._handle_monitor_voice_cleanup(config, monitor, event_id=event_id))
    assert (original / path.name).read_bytes() == b"OggSsynthetic"
    assert monitor.voice_stage(handle) is not None


def test_voice_wrapper_event_recovery_has_no_transcription(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setattr(voice, "_require_root", lambda: None)

    def request(payload):
        requests.append(payload)
        return {"ok": True, "event_id": "inbound-1", "cleanup_verified": True, "stage_active": False}

    monkeypatch.setattr(voice, "_request", request)
    monkeypatch.setattr(voice, "_run_transcriber", lambda *_args, **_kwargs: pytest.fail("no transcription"))
    result = voice.recover_monitored_voice("inbound-1")
    assert result["cleanup_verified"]
    assert requests == [{"operation": "monitor_voice_cleanup", "event_id": "inbound-1"}]
