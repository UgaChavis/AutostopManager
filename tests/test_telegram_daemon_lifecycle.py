"""Whole-process shutdown, using private SQLite and local IPC only."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import TelegramInbox


CHILD = r"""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from autostop_manager import telegram_bridge as bridge

root = Path(sys.argv[1])
mode = sys.argv[2]
bridge.DAEMON_CLEANUP_TIMEOUT_SECONDS = 0.2
bridge.WORK_OWNER_CONFIG_PATH = root / "absent-owner.json"
os.environ[bridge.WORK_DURABLE_ENVIRONMENT] = "1"
config = bridge.TelegramConfig(
    api_id=123456, api_hash="0" * 32,
    session_path=root / "state" / "account", state_dir=root / "state",
    socket_path=root / "run" / "bridge.sock", owner_config_path=bridge.WORK_OWNER_CONFIG_PATH,
)
bridge._config_from_args = lambda _args: config
bridge._configure_durable_instructions = lambda inbox: inbox.configure_instructions(str(root), "a" * 40)
bridge._load_telegram_events = lambda: SimpleNamespace(NewMessage=lambda **_kw: object())
# This fixture has no owner binding and must not require root-owned /etc paths.
bridge._owner_peer_id = lambda *_args, **_kwargs: None

async def stubborn():
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue

class Client:
    def __init__(self, *_args, **_kwargs):
        self.disconnected = asyncio.get_running_loop().create_future()
        self.session = SimpleNamespace(save_entities=True)
        self._sender = SimpleNamespace(_auto_reconnect_callback=self.reconnect)

    async def reconnect(self):
        pass

    def add_event_handler(self, *_args):
        pass

    async def connect(self):
        global client
        client = self

    async def is_user_authorized(self):
        return mode != "auth_disconnect"

    def disconnect(self):
        print("cleanup_entered=true", flush=True)
        if mode in {"shield_disconnect", "auth_disconnect", "bind_failure"}:
            return asyncio.shield(asyncio.create_task(stubborn()))
        if mode == "runner_cancel":
            asyncio.create_task(stubborn())

        async def completed():
            return None

        return completed()

async def reconcile(*_args):
    if mode == "task_cancel":
        await stubborn()
    else:
        await asyncio.Event().wait()

bridge._load_telethon = lambda: (Client, object(), object())
bridge._reconcile_incoming_loop = reconcile
start_server = asyncio.start_unix_server

class HangingServerExit:
    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.server.close()
        print("server_exit_entered=true", flush=True)
        await stubborn()

    async def serve_forever(self):
        await self.server.serve_forever()

async def local_server(*args, **kwargs):
    if mode == "bind_failure":
        raise OSError("synthetic_listener_failure")
    server = await start_server(*args, **kwargs)
    asyncio.get_running_loop().call_soon(
        client.disconnected.set_exception, ConnectionError("synthetic_private_transport_detail")
    )
    return HangingServerExit(server) if mode == "server_exit" else server

bridge.asyncio.start_unix_server = local_server
raise SystemExit(bridge.main(["--account", "work", "daemon", "--monitor-incoming"]))
"""


def _committed_unknown_state(root: Path) -> dict[str, list[tuple]]:
    state = root / "state"
    state.mkdir()
    inbox = TelegramInbox(state / "inbound.sqlite3")
    inbox.configure_instructions(str(root), "a" * 40)
    inbox.duty_set(enabled=True)
    assert inbox.record(42, 100) is not None
    job = inbox.claim(instruction_root=str(root), instruction_revision="a" * 40)
    assert job is not None

    def binding():
        return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}

    job = inbox.start_intent(**binding(), thread_id="synthetic-worker")
    job = inbox.bind_turn(**binding(), turn_id="synthetic-turn")
    effect = inbox.reserve_effect(**binding(), kind="result", text_sha256="b" * 64, peer_id=42, reply_to_message_id=100)
    inbox.mark_effect_intent(effect)
    inbox.close()
    (state / "known-send-ledger.json").write_bytes(b'{"synthetic_known_sent":true}\n')
    return _durable_rows(root)


def _durable_rows(root: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(f"file:{root / 'state' / 'inbound.sqlite3'}?mode=ro", uri=True) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return {table: database.execute(f"SELECT * FROM {table}").fetchall() for table in ("jobs", "events", "effects")}


@pytest.mark.parametrize(
    "mode", ["shield_disconnect", "task_cancel", "runner_cancel", "server_exit", "auth_disconnect", "bind_failure"]
)
def test_cli_cleanup_deadline_exits_entire_process_and_preserves_unknown_intent(tmp_path, mode):
    previous = _committed_unknown_state(tmp_path)
    script = tmp_path / "daemon-child.py"
    script.write_text(CHILD)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), mode],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert time.monotonic() - started < 4
    assert result.stderr == "telegram_bridge_cleanup_timeout=true\n"
    expected_entry = "server_exit_entered=true" if mode == "server_exit" else "cleanup_entered=true"
    if mode != "task_cancel":
        assert expected_entry in result.stdout
    assert not (tmp_path / "run" / "bridge.sock").exists()
    assert _durable_rows(tmp_path) == previous
    assert (tmp_path / "state" / "known-send-ledger.json").read_bytes() == b'{"synthetic_known_sent":true}\n'


def test_cli_successful_cleanup_reports_fixed_failure_without_forcing_exit(tmp_path):
    previous = _committed_unknown_state(tmp_path)
    script = tmp_path / "daemon-child.py"
    script.write_text(CHILD)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "normal"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert '"error": "telegram_transport_disconnected"' in result.stdout
    assert "cleanup_entered=true" in result.stdout
    assert result.stderr == ""
    assert not (tmp_path / "run" / "bridge.sock").exists()
    assert _durable_rows(tmp_path) == previous


def test_cleanup_never_unlinks_replacement_socket_path(tmp_path):
    path = tmp_path / "bridge.sock"
    path.touch()
    original = path.lstat()
    # Rename keeps the original inode occupied, so its replacement is distinct.
    path.rename(tmp_path / "old-socket")
    path.write_text("replacement")
    bridge._unlink_daemon_socket(path, (original.st_dev, original.st_ino))
    assert path.read_text() == "replacement"
    current = path.lstat()
    bridge._unlink_daemon_socket(path, (current.st_dev, current.st_ino))
    assert not path.exists()
    bridge._unlink_daemon_socket(path, (current.st_dev, current.st_ino))
