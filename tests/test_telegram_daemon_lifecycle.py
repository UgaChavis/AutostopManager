"""Daemon startup and whole-process shutdown using private SQLite and local IPC."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


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
bridge._load_telegram_events = lambda: SimpleNamespace(
    NewMessage=lambda **_kw: object(), MessageEdited=lambda: object(), MessageDeleted=lambda: object()
)
# This fixture has no owner binding and must not require root-owned /etc paths.
bridge._owner_peer_id = lambda *_args, **_kwargs: None

selected_stage = {
    "shield_disconnect": "disconnect", "auth_disconnect": "disconnect",
    "bind_failure": "disconnect", "normal": "disconnect",
    "task_cancel": "task_cancel", "runner_cancel": "runner_cancel",
    "server_exit": "server_exit",
}.get(mode)
requested_watchdog = None
stage_entered = False
start_watchdog = bridge._start_daemon_cleanup_watchdog

def request_watchdog(watchdog):
    global requested_watchdog
    assert requested_watchdog is None or requested_watchdog is watchdog
    requested_watchdog = watchdog
    if stage_entered:
        start_watchdog(watchdog)

def enter_stage(stage, marker):
    global stage_entered
    if stage == selected_stage:
        assert requested_watchdog is not None, "cleanup_watchdog_not_requested_before_stage"
        if stage_entered:
            return
        print(marker, flush=True)
        stage_entered = True
        start_watchdog(requested_watchdog)
    else:
        print(marker, flush=True)

# Keep the real Timer and process-exit callback, but budget the selected hang
# after its entry marker rather than after unrelated SQLite cleanup. The
# slow_effect_close control below keeps the original, ungated production start.
if selected_stage is not None:
    bridge._start_daemon_cleanup_watchdog = request_watchdog

reconcile_started = asyncio.Event()

async def stubborn(stage=None, started=None):
    if started is not None:
        started.set()
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if stage is not None:
                enter_stage(stage, stage + "_entered=true")
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

    async def get_me(self):
        return SimpleNamespace(id=900001)

    def disconnect(self):
        enter_stage("disconnect", "cleanup_entered=true")
        if mode in {"shield_disconnect", "auth_disconnect", "bind_failure"}:
            return asyncio.shield(asyncio.create_task(stubborn()))
        runner_started = None
        if mode == "runner_cancel":
            runner_started = asyncio.Event()
            asyncio.create_task(stubborn("runner_cancel", runner_started))

        async def completed():
            if runner_started is not None:
                await runner_started.wait()

        return completed()

async def reconcile(*_args):
    if mode == "task_cancel":
        await stubborn("task_cancel", reconcile_started)
    else:
        await asyncio.Event().wait()

bridge._load_telethon = lambda: (Client, object(), object())
bridge._reconcile_incoming_loop = reconcile
start_server = asyncio.start_unix_server

class HangingServerExit:
    def __init__(self, server):
        self.server = server

    def close(self):
        self.server.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.server.close()
        enter_stage("server_exit", "server_exit_entered=true")
        await stubborn()

    async def serve_forever(self):
        await self.server.serve_forever()

async def local_server(*args, **kwargs):
    if mode == "bind_failure":
        raise OSError("synthetic_listener_failure")
    server = await start_server(*args, **kwargs)
    if mode == "task_cancel":
        async def fail_after_reconcile_started():
            await reconcile_started.wait()
            client.disconnected.set_exception(ConnectionError("synthetic_private_transport_detail"))

        asyncio.create_task(fail_after_reconcile_started())
    else:
        asyncio.get_running_loop().call_soon(
            client.disconnected.set_exception, ConnectionError("synthetic_private_transport_detail")
        )
    return HangingServerExit(server) if mode == "server_exit" else server

bridge.asyncio.start_unix_server = local_server
if mode == "slow_effect_close":
    close_effect_executor = bridge.EffectExecutor.close

    async def delayed_effect_close(self):
        await asyncio.sleep(0.3)
        await close_effect_executor(self)

    bridge.EffectExecutor.close = delayed_effect_close
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

    mapping = inbox.prepare_peer_thread(**binding())
    inbox.bind_peer_thread(**binding(), creation_key=mapping["creation_key"], thread_id="synthetic-worker")
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
    expected_entry = {
        "server_exit": "server_exit_entered=true",
        "task_cancel": "task_cancel_entered=true",
        "runner_cancel": "runner_cancel_entered=true",
    }.get(mode, "cleanup_entered=true")
    assert expected_entry in result.stdout
    if mode == "runner_cancel":
        assert "cleanup_entered=true" in result.stdout
    assert not (tmp_path / "run" / "bridge.sock").exists()
    assert _durable_rows(tmp_path) == previous
    assert (tmp_path / "state" / "known-send-ledger.json").read_bytes() == b'{"synthetic_known_sent":true}\n'


def test_cli_ungated_cleanup_deadline_includes_pre_disconnect_work(tmp_path):
    previous = _committed_unknown_state(tmp_path)
    script = tmp_path / "daemon-child.py"
    script.write_text(CHILD)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "slow_effect_close"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert time.monotonic() - started < 4
    assert result.stderr == "telegram_bridge_cleanup_timeout=true\n"
    assert result.stdout == ""
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


@pytest.mark.parametrize("monitor_incoming", [False, True])
def test_duplicate_durable_startup_preserves_incumbent_listener(tmp_path, monkeypatch, monitor_incoming):
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            calls.append("constructed")

    monkeypatch.setenv(bridge.WORK_DURABLE_ENVIRONMENT, "1")
    monkeypatch.setattr(bridge, "_load_telethon", lambda: (Client, None, None))
    with TemporaryDirectory(prefix="tg-socket-", dir="/tmp") as directory:
        path = Path(directory) / "s"
        config = bridge.TelegramConfig(
            123456,
            "0" * 32,
            state_dir=tmp_path / "state",
            session_path=tmp_path / "state" / "account",
            socket_path=path,
            owner_config_path=bridge.WORK_OWNER_CONFIG_PATH,
        )
        with (
            TelegramInbox(config.state_dir / "inbound.sqlite3") as incumbent,
            socket.socket(socket.AF_UNIX) as listener,
        ):
            listener.bind(str(path))
            listener.listen(2)
            listener.settimeout(1)
            original = path.lstat()
            with pytest.raises(InboxError, match=r"^inbox_writer_active$"):
                asyncio.run(bridge.run_daemon(config, monitor_incoming=monitor_incoming))
            current = path.lstat()
            assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
            with socket.socket(socket.AF_UNIX) as probe:
                probe.connect(str(path))
                accepted, _ = listener.accept()
                accepted.close()
            assert incumbent.health() == {} and calls == []
            with pytest.raises(InboxError, match=r"^inbox_writer_active$"):
                TelegramInbox(config.state_dir / "inbound.sqlite3")


@pytest.mark.parametrize("failure", ["constructor", "handler"])
def test_startup_failure_releases_writer_and_preserves_old_socket(tmp_path, monkeypatch, failure):
    path = tmp_path / "bridge.sock"
    path.write_text("previous listener")
    config = bridge.TelegramConfig(
        123456,
        "0" * 32,
        state_dir=tmp_path / "state",
        session_path=tmp_path / "state" / "account",
        socket_path=path,
        owner_config_path=bridge.WORK_OWNER_CONFIG_PATH,
    )
    disconnected = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            with pytest.raises(InboxError, match=r"^inbox_writer_active$"):
                with TelegramInbox(config.state_dir / "inbound.sqlite3"):
                    pass
            if failure == "constructor":
                raise OSError("synthetic startup failure")
            self.session = SimpleNamespace(save_entities=True)

        def add_event_handler(self, *_args):
            raise OSError("synthetic startup failure")

        async def disconnect(self):
            disconnected.append(True)

    monkeypatch.setenv(bridge.WORK_DURABLE_ENVIRONMENT, "1")
    monkeypatch.setattr(bridge, "_load_telethon", lambda: (Client, None, None))
    monkeypatch.setattr(bridge, "_configure_durable_instructions", lambda _inbox: None)
    monkeypatch.setattr(bridge, "_install_reconnect_signal", lambda *_args: None)
    monkeypatch.setattr(
        bridge,
        "_load_telegram_events",
        lambda: SimpleNamespace(
            NewMessage=lambda **_kwargs: object(), MessageEdited=lambda: object(), MessageDeleted=lambda: object()
        ),
    )
    with pytest.raises(OSError, match=r"^synthetic startup failure$"):
        asyncio.run(bridge.run_daemon(config, monitor_incoming=True))
    with TelegramInbox(config.state_dir / "inbound.sqlite3") as restored:
        assert restored.status()["open_events"] == 0
    assert path.read_text() == "previous listener"
    assert disconnected == ([] if failure == "constructor" else [True])
