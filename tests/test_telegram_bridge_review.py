from __future__ import annotations

import asyncio
import ast
from datetime import UTC, datetime
import errno
import hashlib
import inspect
import json
from pathlib import Path
import socket
import subprocess
import struct
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


class MemoryWriter:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        pass


class MemorySocket:
    def __init__(self, response, path):
        self.response = response
        self.path = path
        self.received = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def settimeout(self, value):
        assert 0 < value <= bridge.DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS

    def connect(self, path):
        assert Path(path) == self.path

    def sendall(self, data):
        assert isinstance(json.loads(data), dict)

    def recv(self, size):
        result, self.response = self.response[:size], self.response[size:]
        self.received += len(result)
        return result


@pytest.fixture(autouse=True)
def isolated_effects(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    for key, child in (
        ("AUTOSTOP_MANAGER_DB", "manager.sqlite3"),
        ("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", "runtime-unavailable"),
        ("AUTOSTOP_J1_CACHE_DIR", "j1"),
        ("AUTOSTOP_J1_VIN_CACHE_DIR", "vin"),
        ("HF_HOME", "model-cache"),
    ):
        monkeypatch.setenv(key, str(tmp_path / child))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv(bridge.WORK_WAKE_ENVIRONMENT, raising=False)
    monkeypatch.setenv(bridge.WORK_DURABLE_ENVIRONMENT, "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("real account/session/network/process/config operations forbidden")

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(bridge, "_load_telethon", forbidden)
    monkeypatch.setattr(bridge, "_config_for_account", forbidden)
    monkeypatch.setattr(bridge, "_owner_peer_id", forbidden)
    monkeypatch.setattr(bridge, "load_owner_peer_id", forbidden)


@pytest.fixture
def config(tmp_path):
    return bridge.TelegramConfig(
        123456,
        "0" * 32,
        session_path=tmp_path / "state" / "account",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "bridge.sock",
    )


async def serve_request(client, config, request):
    reader = asyncio.StreamReader()
    reader.feed_data(bridge._canonical_json(request) + b"\n")
    writer = MemoryWriter()
    await bridge._serve_client_request(client, config, reader, writer)
    assert writer.closed
    return bytes(writer.output)


def receive(monkeypatch, config, response, request=None):
    connection = MemorySocket(response, config.socket_path)
    with monkeypatch.context() as ipc:
        ipc.setattr(bridge.socket, "socket", lambda *_args, **_kwargs: connection)
        try:
            result = bridge.send_local_request(config.socket_path, request or {"operation": "read"})
        finally:
            assert connection.closed
    return result, connection


@pytest.mark.parametrize("count", [20, 100])
@pytest.mark.parametrize("character", ["A", "Я", "🚗", "\x00"])
def test_complete_read_batch_survives_utf8_and_json_expansion(monkeypatch, config, count, character):
    text = character * bridge.MAX_MESSAGE_CHARS
    messages = [
        SimpleNamespace(
            id=index + 1,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            out=False,
            message=text,
            media=None,
            reply_to=None,
        )
        for index in range(count)
    ]

    class Client:
        async def get_messages(self, entity, *, limit):
            assert entity == "synthetic-peer" and limit == count
            return list(reversed(messages))

    async def resolve(_client, peer):
        assert peer == "42"
        return "synthetic-peer", {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = {"operation": "read", "peer": "42", "limit": count}
    response = asyncio.run(serve_request(Client(), config, request))

    result, connection = receive(monkeypatch, config, response, request)

    assert result["ok"] is True
    assert [row["id"] for row in result["messages"]] == list(range(1, count + 1))
    assert all(row["text"] == text for row in result["messages"])
    assert connection.received == len(response)


def test_response_at_wire_boundary_is_accepted(monkeypatch, config):
    limit = getattr(bridge, "MAX_RESPONSE_BYTES", bridge.MAX_REQUEST_BYTES)
    overhead = len(bridge._canonical_json({"ok": True, "data": ""}) + b"\n")
    response = bridge._canonical_json({"ok": True, "data": "A" * (limit - overhead)}) + b"\n"
    assert len(response) == limit

    result, connection = receive(monkeypatch, config, response)

    assert result["ok"] is True
    assert connection.received == limit


def test_response_overflow_stops_after_one_detection_byte_and_closes(monkeypatch, config):
    limit = getattr(bridge, "MAX_RESPONSE_BYTES", bridge.MAX_REQUEST_BYTES)
    response = bridge._canonical_json({"ok": True, "data": "A" * (limit + 65536)}) + b"\n"
    connection = MemorySocket(response, config.socket_path)
    monkeypatch.setattr(bridge.socket, "socket", lambda *_args, **_kwargs: connection)

    with pytest.raises(bridge.BridgeError, match="response_invalid"):
        bridge.send_local_request(config.socket_path, {"operation": "read"})

    assert connection.closed
    assert connection.received <= limit + 1


@pytest.mark.parametrize("response", [b"", b'{"ok":true}', b"{broken}\n", b"[]\n", b"\xff\n"])
def test_incomplete_or_invalid_response_frame_is_rejected(monkeypatch, config, response):
    connection = MemorySocket(response, config.socket_path)
    monkeypatch.setattr(bridge.socket, "socket", lambda *_args, **_kwargs: connection)

    with pytest.raises(bridge.BridgeError, match="response_invalid"):
        bridge.send_local_request(config.socket_path, {"operation": "read"})

    assert connection.closed


def test_only_the_first_response_frame_is_consumed(monkeypatch, config):
    result, _connection = receive(monkeypatch, config, b'{"ok":true}\n' + b"unrelated trailing bytes")
    assert result == {"ok": True}


def test_oversize_request_is_rejected_before_any_connection(monkeypatch, config):
    connections = []
    monkeypatch.setattr(bridge.socket, "socket", lambda *_args, **_kwargs: connections.append(1))

    with pytest.raises(bridge.BridgeError, match="request_size_invalid"):
        bridge.send_local_request(config.socket_path, {"operation": "read", "padding": "A" * bridge.MAX_REQUEST_BYTES})

    assert connections == []


def test_server_refuses_oversized_response_with_a_bounded_error(monkeypatch, config):
    limit = getattr(bridge, "MAX_RESPONSE_BYTES", bridge.MAX_REQUEST_BYTES)

    async def operation(*_args, **_kwargs):
        return {"ok": True, "data": "A" * (limit + 1)}

    monkeypatch.setattr(bridge, "_handle_operation", operation)
    response = asyncio.run(serve_request(object(), config, {"operation": "read"}))
    result = json.loads(response)

    assert result["ok"] is False
    assert result["error"] == "response_invalid"
    assert len(response) <= limit


def legacy_import_request(count):
    return {
        "operation": "job_import_legacy",
        "events": [
            {
                "event_id": f"inbound-{index + 1}",
                "peer_id": 42,
                "message_id": index + 1,
                "received_at": "2026-01-01T00:00:00+00:00",
                "state": "open",
            }
            for index in range(count)
        ],
        "instruction_root": "/synthetic/instructions",
        "instruction_revision": "0" * 40,
    }


async def serve_through_native_factory(config, request):
    """Exercise the published factory registration and RPC without a listener."""

    class Inbox:
        def __init__(self):
            self.imports = []

        def import_references(self, rows, *, instruction_root, instruction_revision):
            assert instruction_root == "/synthetic/instructions"
            assert instruction_revision == "0" * 40
            bridge.TelegramInbox._instruction(instruction_root, instruction_revision)
            published = inspect.getmodule(bridge.TelegramInbox)
            seen = set()
            for row in rows:
                identity = bridge.TelegramInbox._event_sequence(row["event_id"])
                assert identity not in seen
                seen.add(identity)
                published._positive(row["peer_id"], "inbound_peer_invalid")
                published._positive(row["message_id"], "inbound_message_invalid")
                published._timestamp(row["received_at"])
                assert row["state"] == "open"
            self.imports.append(len(rows))
            return {"imported": len(rows), "event_ids": [row["event_id"] for row in rows], "review_job_ids": []}

        def job_status(self):
            return {"synthetic": True}

    class RootWriter(MemoryWriter):
        def get_extra_info(self, name):
            assert name == "socket"
            return SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 1, 0, 0))

    inbox = Inbox()
    monitor = object.__new__(bridge.DurableInboundMonitor)
    monitor.inbox = inbox
    monitor.changed = lambda: None
    payload = bridge._canonical_json(request) + b"\n"
    factory_default = inspect.signature(asyncio.start_unix_server).parameters["limit"].default

    async def factory(callback, *, path, limit=factory_default):
        assert Path(path) == config.socket_path
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(payload)
        reader.feed_eof()
        writer = RootWriter()
        await callback(reader, writer)
        assert writer.closed
        return json.loads(writer.output)

    # Only this native call is evaluated. Account/session initialization,
    # daemon lifecycle and socket creation are excluded by construction.
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(bridge.run_daemon)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "start_unix_server"
    ]
    assert len(calls) == 1
    expression = ast.fix_missing_locations(ast.Expression(body=calls[0]))
    namespace = bridge.run_daemon.__globals__ | {
        "asyncio": SimpleNamespace(start_unix_server=factory),
        "client": object(),
        "config": config,
        "inbound_monitor": monitor,
        "handlers": bridge._DaemonRpcHandlers(object(), config, monitor),
    }
    result = await eval(compile(expression, "<native bridge factory>", "eval"), namespace)
    return result, inbox.imports


@pytest.mark.parametrize("count", [3, 600])
def test_native_factory_routes_allowed_legacy_import_batches(config, count):
    request = legacy_import_request(count)
    wire_bytes = len(bridge._canonical_json(request) + b"\n")
    assert wire_bytes < bridge.MAX_REQUEST_BYTES
    if count == 600:
        assert wire_bytes > 65536

    response, imports = asyncio.run(serve_through_native_factory(config, request))

    assert response["ok"] is True and response["imported"] == count
    assert imports == [count]
    if count == 600:
        # The accepted wire boundary includes the delimiter, as on the caller.
        request["padding"] = ""
        request["padding"] = "A" * (bridge.MAX_REQUEST_BYTES - len(bridge._canonical_json(request) + b"\n"))
        assert len(bridge._canonical_json(request) + b"\n") == bridge.MAX_REQUEST_BYTES
        response, imports = asyncio.run(serve_through_native_factory(config, request))
        assert response["ok"] is True and response["imported"] == count
        assert imports == [count]


def test_native_factory_refuses_over_cap_request_before_legacy_import(config):
    request = legacy_import_request(3) | {"padding": ""}
    request["padding"] = "A" * (bridge.MAX_REQUEST_BYTES + 1 - len(bridge._canonical_json(request) + b"\n"))
    assert len(bridge._canonical_json(request) + b"\n") == bridge.MAX_REQUEST_BYTES + 1

    response, imports = asyncio.run(serve_through_native_factory(config, request))

    assert response["ok"] is False and response["error"] in {"request_invalid", "request_size_invalid"}
    assert imports == []


def voice_runtime(config):
    monitor = bridge.InboundMonitor(max_events=1)
    event_id = monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=42, id=100))
    audio = b"OggSsynthetic-voice"
    attribute = type("DocumentAttributeAudio", (), {})()
    attribute.voice, attribute.duration = True, 8
    message = SimpleNamespace(
        id=100,
        out=False,
        is_private=True,
        chat_id=42,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        message="",
        media=object(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[attribute]),
    )
    return monitor, event_id, audio, message


@pytest.mark.parametrize("state_change", ["closed", "evicted"])
@pytest.mark.parametrize("unlink_fails", [False, True])
def test_stale_voice_event_never_writes_after_await(monkeypatch, config, state_change, unlink_fails):
    monitor, event_id, audio, message = voice_runtime(config)
    saves, scheduled = [], []
    original_save = bridge._save_monitor_voice_stage
    original_unlink = Path.unlink

    def save(*args, **kwargs):
        saves.append(1)
        return original_save(*args, **kwargs)

    def unlink(path, *args, **kwargs):
        if unlink_fails and path.parent == config.socket_path.parent / "inbox" and path.name.startswith("monitor-"):
            raise OSError(errno.EIO, "synthetic deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(bridge, "_save_monitor_voice_stage", save)
    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: scheduled.append(1))
    monkeypatch.setattr(Path, "unlink", unlink)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class Client:
            async def get_messages(self, entity, *, ids):
                assert entity is None and ids == 100
                return message

            async def download_media(self, actual_message, *, file):
                assert actual_message is message and file is bytes
                entered.set()
                await release.wait()
                return audio

        task = asyncio.create_task(bridge._handle_monitor_voice_stage(Client(), config, monitor, event_id=event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if state_change == "closed":
                monitor.mark_no_reply_needed(event_id)
                error = "inbound_event_not_open"
            else:
                monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=42, id=101))
                error = "inbound_event_unavailable"
            release.set()
            with pytest.raises(bridge.BridgeError, match=error):
                await asyncio.wait_for(task, 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert saves == scheduled == []
    assert not list((config.socket_path.parent / "inbox").glob("monitor-*"))
    assert not monitor._voice_stage_reservations


def test_open_voice_stages_once_and_retains_cleanup_handle_until_verified(monkeypatch, config):
    monitor, event_id, audio, message = voice_runtime(config)
    scheduled = []

    class Client:
        async def get_messages(self, entity, *, ids):
            assert entity is None and ids == 100
            return message

        async def download_media(self, actual_message, *, file):
            assert actual_message is message and file is bytes
            return audio

    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: scheduled.append(1))
    result = asyncio.run(bridge._handle_monitor_voice_stage(Client(), config, monitor, event_id=event_id))
    handle = result["media_handle"]
    stage = monitor.voice_stage(handle)
    assert stage is not None and stage.path.read_bytes() == audio and scheduled == [1]
    original_unlink = bridge.os.unlink

    def unavailable(path, *args, **kwargs):
        if path == stage.path.name and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EIO, "synthetic deletion failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(bridge.os, "unlink", unavailable)
        with pytest.raises((OSError, bridge.BridgeError)):
            asyncio.run(bridge._handle_monitor_voice_discard(config, monitor, handle=handle))
    assert monitor.voice_stage(handle) is stage and stage.path.exists()

    cleanup = asyncio.run(bridge._handle_monitor_voice_discard(config, monitor, handle=handle))

    assert cleanup == {"ok": True, "cleanup_verified": True}
    assert monitor.voice_stage(handle) is None and not stage.path.exists()
    assert monitor.require_open(event_id)


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_voice_cancel_or_timeout_releases_claim_without_saving(monkeypatch, config, mode):
    monitor, event_id, _audio, message = voice_runtime(config)
    saves = []
    monkeypatch.setattr(bridge, "_save_monitor_voice_stage", lambda *_args, **_kwargs: saves.append(1))
    if mode == "timeout":
        monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TIMEOUT_SECONDS", 0.01)

    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Client:
            async def get_messages(self, entity, *, ids):
                return message

            async def download_media(self, _message, *, file):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        task = asyncio.create_task(bridge._handle_monitor_voice_stage(Client(), config, monitor, event_id=event_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(bridge.BridgeError, match="inbound_voice_download_timeout"):
                    await asyncio.wait_for(task, 1)
            assert cancelled.is_set()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert saves == []
    event = monitor.require_open(event_id)
    monitor.reserve_voice_stage(event)
    monitor.release_voice_stage_reservation(event)


@pytest.mark.parametrize("kind", ["voice", "download"])
def test_committed_media_is_returned_without_redundant_temporary_cleanup(monkeypatch, config, kind):
    inbox = config.socket_path.parent / "inbox"
    content = b"OggSsynthetic-committed-media"
    unnecessary_cleanup = []
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.parent == inbox and path.name.startswith(".") and path.suffix == ".tmp":
            unnecessary_cleanup.append(1)
            raise OSError(errno.EIO, "synthetic post-rename temporary cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    if kind == "voice":
        output = bridge._save_monitor_voice_stage(content, handle="m" * 32, suffix=".ogg", inbox_dir=inbox)
        with pytest.raises(bridge.BridgeError, match="inbound_voice_stage_conflict"):
            bridge._save_monitor_voice_stage(content, handle="m" * 32, suffix=".ogg", inbox_dir=inbox)
    else:
        output, digest, owned_inode = bridge._save_private_download(
            content, message_id=100, suffix=".ogg", inbox_dir=inbox
        )
        assert digest == hashlib.sha256(content).hexdigest()
        assert owned_inode == (output.stat().st_dev, output.stat().st_ino)
        replay = bridge._save_private_download(content, message_id=100, suffix=".ogg", inbox_dir=inbox)
        assert replay == (output, digest, None)

    assert output.read_bytes() == content
    assert output.stat().st_mode & 0o777 == 0o600
    assert unnecessary_cleanup == []
    assert not list(inbox.glob(".*.tmp"))


@pytest.mark.parametrize("kind", ["voice", "download"])
def test_media_write_failure_before_commit_cleans_temporary_and_preserves_error(monkeypatch, config, kind):
    inbox = config.socket_path.parent / "inbox"

    def fsync(_fd):
        raise OSError(errno.ENOSPC, "synthetic pre-commit fsync failure")

    monkeypatch.setattr(bridge.os, "fsync", fsync)
    with pytest.raises(OSError) as error:
        if kind == "voice":
            bridge._save_monitor_voice_stage(b"OggSsynthetic", handle="m" * 32, suffix=".ogg", inbox_dir=inbox)
        else:
            bridge._save_private_download(b"OggSsynthetic", message_id=100, suffix=".ogg", inbox_dir=inbox)

    assert error.value.errno == errno.ENOSPC
    assert list(inbox.iterdir()) == []


def staged_voice(config):
    monitor, event_id, audio, _message = voice_runtime(config)
    event = monitor.require_open(event_id)
    monitor.reserve_voice_stage(event)
    handle = "m" * 32
    path = bridge._save_monitor_voice_stage(
        audio, handle=handle, suffix=".ogg", inbox_dir=config.socket_path.parent / "inbox"
    )
    monitor.stage_voice(event, handle, path, duration_seconds=8, suffix=".ogg")
    return monitor, handle, path


@pytest.mark.parametrize("failure_count", [1, None])
@pytest.mark.parametrize("error_kind", ["os", "bridge"])
def test_expiry_retries_owned_media_deletion_and_preserves_permanent_failure(
    monkeypatch, config, failure_count, error_kind
):
    monitor, handle, path = staged_voice(config)
    attempts = []
    original_unlink = bridge.os.unlink
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_CLEANUP_RETRY_SECONDS", 0)
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_CLEANUP_RETRIES", 3)

    def unlink(actual_path, *args, **kwargs):
        if actual_path == path.name and kwargs.get("dir_fd") is not None:
            attempts.append(1)
            if failure_count is None or len(attempts) <= failure_count:
                if error_kind == "os":
                    raise OSError(errno.EIO, "synthetic output deletion failure")
                raise bridge.BridgeError("download_unavailable")
        return original_unlink(actual_path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(bridge.os, "unlink", unlink)
        asyncio.run(bridge._expire_monitor_voice_stage(config, monitor, handle))

    assert len(attempts) == (2 if failure_count == 1 else 3)
    if failure_count == 1:
        assert monitor.voice_stage(handle) is None and not path.exists()
    else:
        assert monitor.voice_stage(handle) is not None and path.exists()
        result = asyncio.run(bridge._handle_monitor_voice_discard(config, monitor, handle=handle))
        assert result == {"ok": True, "cleanup_verified": True}
        assert monitor.voice_stage(handle) is None and not path.exists()


def test_expiry_cancellation_preserves_owned_file_and_manual_cleanup_handle(monkeypatch, config):
    monitor, handle, path = staged_voice(config)

    async def run():
        entered = asyncio.Event()

        async def pending_sleep(_delay):
            entered.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as waiting:
            waiting.setattr(bridge.asyncio, "sleep", pending_sleep)
            task = asyncio.create_task(bridge._expire_monitor_voice_stage(config, monitor, handle))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

    assert monitor.voice_stage(handle) is not None and path.exists()
    assert asyncio.run(bridge._handle_monitor_voice_discard(config, monitor, handle=handle))["cleanup_verified"]
    assert monitor.voice_stage(handle) is None and not path.exists()


def test_expiry_and_discard_never_delete_media_outside_configured_inbox(monkeypatch, config):
    monitor, event_id, _audio, _message = voice_runtime(config)
    outside = config.state_dir / "outside.ogg"
    outside.parent.mkdir(parents=True, mode=0o700)
    outside.write_bytes(b"OggSsynthetic-outside")
    outside.chmod(0o600)
    event = monitor.require_open(event_id)
    monitor.reserve_voice_stage(event)
    handle = "m" * 32
    monitor.stage_voice(event, handle, outside, duration_seconds=8, suffix=".ogg")
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_TTL_SECONDS", 0)
    monkeypatch.setattr(bridge, "MONITOR_VOICE_STAGE_CLEANUP_RETRY_SECONDS", 0)

    asyncio.run(bridge._expire_monitor_voice_stage(config, monitor, handle))
    with pytest.raises(bridge.BridgeError, match="download_path_invalid"):
        asyncio.run(bridge._handle_monitor_voice_discard(config, monitor, handle=handle))

    assert outside.read_bytes() == b"OggSsynthetic-outside"
    assert monitor.voice_stage(handle) is not None
