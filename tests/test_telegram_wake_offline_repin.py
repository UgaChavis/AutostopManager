"""Offline pin update never starts a Codex or Telegram service."""

import asyncio
import hashlib
import json
import os
import sys

import pytest
from instruction_bundle_fakes import create_instruction_bundle
from telegram_wake_fakes import THREAD

from autostop_manager import telegram_wake as wake


@pytest.fixture
def pinned_setup(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("installed wake config is root-owned")
    releases = tmp_path / "releases"
    old = create_instruction_bundle(releases / "old", "a" * 40)
    new = create_instruction_bundle(releases / "new", "b" * 40)
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (releases,))
    config_path = tmp_path / "wake.json"
    config_path.write_text(
        json.dumps(
            {
                "thread_id": THREAD,
                "project_dir": wake.PROJECT_DIR,
                "app_socket": wake.APP_SOCKET,
                "instruction_root": str(old),
                "instruction_revision": "a" * 40,
            }
        )
    )
    config_path.chmod(0o600)
    monkeypatch.setattr(wake, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wake, "SOCKET_PATH", tmp_path / "run" / "wake.sock")

    async def no_codex(*_args, **_kwargs):
        pytest.fail("offline setup must not connect to Codex")

    monkeypatch.setattr(wake.AppServer, "connect", no_codex)
    return config_path, old, new


def test_offline_repin_preserves_worker_and_uses_atomic_compare_and_swap(pinned_setup):
    config_path, old, new = pinned_setup
    original = config_path.read_bytes()
    old_config = json.loads(original)
    digest = hashlib.sha256(original).hexdigest()

    async def scenario():
        with pytest.raises(wake.WakeError, match="wake_config_changed"):
            await wake.setup_task(
                instruction_root=str(new), instruction_revision="b" * 40, expected_config_sha256="0" * 64
            )
        assert config_path.read_bytes() == original
        result = await wake.setup_task(
            instruction_root=str(new), instruction_revision="b" * 40, expected_config_sha256=digest
        )
        assert result["instruction_revision"] == "b" * 40
        new_config = json.loads(config_path.read_text())
        assert new_config == old_config | {"instruction_root": str(new), "instruction_revision": "b" * 40}
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert wake.WakeConfig.load(config_path).instruction_root == str(new)
        assert old.exists()

    asyncio.run(scenario())


def test_offline_repin_refuses_running_daemon_or_invalid_bundle(pinned_setup):
    config_path, _old, new = pinned_setup
    original = config_path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    wake.SOCKET_PATH.parent.mkdir()
    owner = wake._WakeSocketOwner(wake.SOCKET_PATH)
    owner.acquire()

    async def scenario():
        try:
            with pytest.raises(wake.WakeError, match="wake_daemon_active"):
                await wake.setup_task(
                    instruction_root=str(new), instruction_revision="b" * 40, expected_config_sha256=digest
                )
        finally:
            owner.close()
        (new / "MANIFEST.sha256").unlink()
        with pytest.raises(wake.WakeError, match="wake_instruction_bundle_invalid"):
            await wake.setup_task(
                instruction_root=str(new), instruction_revision="b" * 40, expected_config_sha256=digest
            )
        assert config_path.read_bytes() == original

    asyncio.run(scenario())


def test_offline_repin_rejects_partial_arguments_without_writing(pinned_setup):
    config_path, _old, new = pinned_setup
    original = config_path.read_bytes()

    async def scenario():
        with pytest.raises(wake.WakeError, match="wake_instruction_arguments_required"):
            await wake.setup_task(instruction_root=str(new))
        assert config_path.read_bytes() == original

    asyncio.run(scenario())


def test_setup_cli_repin_preserves_existing_thread(pinned_setup, monkeypatch, capsys):
    config_path, _old, new = pinned_setup
    old = json.loads(config_path.read_text())
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "telegram_wake",
            "setup",
            "--instruction-root",
            str(new),
            "--instruction-revision",
            "b" * 40,
            "--expected-config-sha256",
            digest,
        ],
    )
    assert wake.main() == 0
    assert json.loads(capsys.readouterr().out)["instruction_revision"] == "b" * 40
    assert json.loads(config_path.read_text()) == old | {"instruction_root": str(new), "instruction_revision": "b" * 40}


def test_daemon_loads_configuration_after_acquiring_socket_lock(pinned_setup, monkeypatch):
    wake.SOCKET_PATH.parent.mkdir()

    def load_while_locked(_cls, path):
        assert path == wake.CONFIG_PATH
        contender = wake._WakeSocketOwner(wake.SOCKET_PATH)
        with pytest.raises(wake.WakeError, match="wake_daemon_active"):
            contender.acquire()
        raise wake.WakeError("config_read_under_lock")

    monkeypatch.setattr(wake.WakeConfig, "load", classmethod(load_while_locked))
    with pytest.raises(wake.WakeError, match="config_read_under_lock"):
        asyncio.run(wake.daemon())
    owner = wake._WakeSocketOwner(wake.SOCKET_PATH)
    owner.acquire()
    owner.close()


def test_installed_daemon_rejects_stale_instruction_pin(pinned_setup, monkeypatch):
    config_path, _old, new = pinned_setup
    original = config_path.read_bytes()
    wake.SOCKET_PATH.parent.mkdir()
    monkeypatch.setattr(wake, "__file__", str(new / "autostop_manager" / "telegram_wake.py"))

    async def no_prepare(_owner):
        pytest.fail("stale instructions must be rejected before preparing a listener")

    monkeypatch.setattr(wake._WakeSocketOwner, "prepare", no_prepare)
    with pytest.raises(wake.WakeError, match="wake_instruction_release_mismatch"):
        asyncio.run(wake.daemon())
    assert config_path.read_bytes() == original
    owner = wake._WakeSocketOwner(wake.SOCKET_PATH)
    owner.acquire()
    owner.close()


def test_installed_daemon_accepts_exact_instruction_pin(pinned_setup, monkeypatch):
    config_path, _old, new = pinned_setup
    monkeypatch.setattr(wake, "__file__", str(new / "autostop_manager" / "telegram_wake.py"))
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()

    async def stop_after_pin_verification(_owner):
        raise wake.WakeError("runtime_pin_verified")

    async def scenario():
        await wake.setup_task(instruction_root=str(new), instruction_revision="b" * 40, expected_config_sha256=digest)
        monkeypatch.setattr(wake._WakeSocketOwner, "prepare", stop_after_pin_verification)
        with pytest.raises(wake.WakeError, match="runtime_pin_verified"):
            await wake.daemon()

    asyncio.run(scenario())
