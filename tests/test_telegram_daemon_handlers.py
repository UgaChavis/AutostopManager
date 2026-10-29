"""Own real private Unix handlers; Telegram, owner config and peer ACL are fixtures."""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


class Watchdog:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ident = None
        self.on_start = None

    def start(self):
        self.ident = 1
        self.runtime.events.append("watchdog_armed")
        if self.on_start is not None:
            self.on_start()

    def cancel(self):
        pass


class Client:
    def __init__(self, runtime):
        self.runtime = runtime
        self.disconnected = asyncio.get_running_loop().create_future()
        self.session = SimpleNamespace(save_entities=True)
        self._sender = SimpleNamespace(_auto_reconnect_callback=self.reconnect)
        runtime.client = self

    async def reconnect(self):
        pass

    def add_event_handler(self, *_args):
        pass

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=900001)

    async def disconnect(self):
        if self.runtime.receipt_required:
            assert self.runtime.receipt_saved.is_set()
        self.runtime.events.append("client_disconnect")
        if not self.disconnected.done():
            self.disconnected.set_result(None)


@pytest.fixture
def daemon_runtime(tmp_path, monkeypatch):
    real_start_server = asyncio.start_unix_server
    real_serve_client = bridge._serve_client
    real_install_reconnect = bridge._install_reconnect_signal
    real_inbox_close = bridge.TelegramInbox.close

    @asynccontextmanager
    async def running(*, defer_accept=False, handler_error=False):
        runtime = SimpleNamespace(
            events=[],
            peers=[],
            handler_refs=[],
            accepted=asyncio.Event(),
            listening=asyncio.Event(),
            receipt_saved=asyncio.Event(),
            receipt_required=False,
            on_handler_cancel=None,
            tasks_before=asyncio.all_tasks(),
            inbox=None,
        )

        def configure(inbox):
            runtime.inbox = inbox
            inbox.configure_instructions(str(tmp_path), "a" * 40)

        def install_reconnect(client, monitor):
            runtime.monitor = monitor
            real_install_reconnect(client, monitor)

        def close_inbox(inbox):
            if inbox is runtime.inbox:
                if runtime.receipt_required:
                    assert runtime.receipt_saved.is_set()
                runtime.events.append("inbox_close")
            real_inbox_close(inbox)

        async def serve_client(*args, **kwargs):
            runtime.handler_refs.append(weakref.ref(asyncio.current_task()))
            try:
                await real_serve_client(*args, **kwargs)
            except asyncio.CancelledError:
                runtime.events.append("handler_cancelled")
                if runtime.on_handler_cancel is not None:
                    runtime.on_handler_cancel()
                raise
            if handler_error:
                raise RuntimeError("synthetic handler failure after writer cleanup")

        async def start_server(callback, **kwargs):
            runtime.accept_client = callback

            def incoming(reader, writer):
                runtime.accepted.set()
                if defer_accept:
                    runtime.deferred = (reader, writer)
                    return None
                return callback(reader, writer)

            runtime.server = await real_start_server(incoming, **kwargs)
            runtime.listening.set()
            return runtime.server

        async def open_peer(payload):
            reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
            runtime.peers.append(writer)
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            return reader, writer

        config = bridge.TelegramConfig(
            123456,
            "0" * 32,
            session_path=tmp_path / "state" / "account",
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "bridge.sock",
            owner_config_path=tmp_path / "absent-owner.json",
        )
        runtime.config = config
        runtime.open_peer = open_peer
        runtime.watchdog = Watchdog(runtime)
        monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
        monkeypatch.setenv(bridge.WORK_DURABLE_ENVIRONMENT, "1")
        monkeypatch.setattr(bridge, "WORK_OWNER_CONFIG_PATH", config.owner_config_path)
        monkeypatch.setattr(bridge, "_load_telethon", lambda: (lambda *_args, **_kw: Client(runtime), None, None))
        monkeypatch.setattr(
            bridge,
            "_load_telegram_events",
            lambda: SimpleNamespace(
                NewMessage=lambda **_kw: object(), MessageEdited=lambda: object(), MessageDeleted=lambda: object()
            ),
        )
        monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: None)
        # These IPC lifecycle tests do not exercise peer authorization or a
        # root-owned installed instruction bundle. Production guards stay intact.
        monkeypatch.setattr(bridge, "_require_root_client", lambda _writer: None)
        monkeypatch.setattr(bridge, "_configure_durable_instructions", configure)
        monkeypatch.setattr(bridge, "_install_reconnect_signal", install_reconnect)
        monkeypatch.setattr(bridge.TelegramInbox, "close", close_inbox)
        monkeypatch.setattr(bridge, "_serve_client", serve_client)
        monkeypatch.setattr(bridge.asyncio, "start_unix_server", start_server)
        runtime.daemon = asyncio.create_task(
            bridge.run_daemon(config, monitor_incoming=False, _cleanup_watchdog=runtime.watchdog)
        )
        try:
            await asyncio.wait_for(runtime.listening.wait(), 2)
            yield runtime
        finally:
            for writer in runtime.peers:
                writer.close()
            for writer in runtime.peers:
                with suppress(ConnectionError):
                    await writer.wait_closed()
            if not runtime.daemon.done():
                runtime.daemon.cancel()
            done, _ = await asyncio.wait({runtime.daemon}, timeout=2)
            assert done, "private peers are closed, so fixture cleanup must finish"
            with suppress(asyncio.CancelledError, bridge.BridgeError):
                await runtime.daemon

    return running


async def stop_daemon(runtime, trigger):
    if trigger == "cancel":
        runtime.daemon.cancel()
    else:
        runtime.client.disconnected.set_exception(ConnectionError("synthetic transport disconnect"))
    done, _ = await asyncio.wait({runtime.daemon}, timeout=2)
    assert done, "daemon must finish before the still-open private peer sends EOF"
    if trigger == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runtime.daemon
    else:
        with pytest.raises(bridge.BridgeError, match=r"^telegram_transport_disconnected$"):
            await runtime.daemon
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.parametrize("trigger", ["disconnect", "cancel"])
def test_shutdown_closes_healthy_subscription_and_only_owned_handlers(daemon_runtime, trigger):
    async def scenario():
        async with daemon_runtime() as runtime:
            reader, writer = await runtime.open_peer({"operation": "job_subscribe", "heartbeat": True})
            frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
            assert frame == {"ok": True, "generation": 1, "heartbeat": False}
            assert len(runtime.monitor.subscribers) == 1
            unrelated = asyncio.create_task(asyncio.Event().wait())
            try:
                await stop_daemon(runtime, trigger)
                assert not writer.is_closing()
                assert await asyncio.wait_for(reader.read(), 2) == b""
                assert not runtime.monitor.subscribers
                assert bridge._effect_executor().closed
                assert runtime.events.count("inbox_close") == runtime.events.count("client_disconnect") == 1
                assert not unrelated.done()
                assert asyncio.all_tasks() == runtime.tasks_before | {unrelated}
            finally:
                unrelated.cancel()
                with suppress(asyncio.CancelledError):
                    await unrelated

    asyncio.run(scenario())


@pytest.mark.parametrize("admission", ["before_first_step", "after_stop"])
def test_shutdown_closes_accepted_writer_even_when_handler_never_starts(daemon_runtime, admission):
    async def scenario():
        async with daemon_runtime(defer_accept=True) as runtime:
            reader, writer = await runtime.open_peer({"operation": "job_status"})
            await asyncio.wait_for(runtime.accepted.wait(), 2)

            def admit():
                runtime.accept_client(*runtime.deferred)

            runtime.watchdog.on_start = (
                admit if admission == "before_first_step" else lambda: asyncio.get_running_loop().call_soon(admit)
            )
            await stop_daemon(runtime, "disconnect")
            assert not writer.is_closing()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert not runtime.handler_refs
            assert runtime.server._active_count == 0
            assert asyncio.all_tasks() == runtime.tasks_before

    asyncio.run(scenario())


def test_cancelled_cold_handler_closes_writer_before_daemon_shutdown(daemon_runtime):
    async def scenario():
        async with daemon_runtime(defer_accept=True) as runtime:
            reader, writer = await runtime.open_peer({"operation": "job_status"})
            await asyncio.wait_for(runtime.accepted.wait(), 2)
            handler = runtime.accept_client(*runtime.deferred)
            handler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handler
            assert not runtime.handler_refs and not runtime.daemon.done()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert not writer.is_closing() and runtime.server._active_count == 0
            await stop_daemon(runtime, "cancel")
            assert asyncio.all_tasks() == runtime.tasks_before

    asyncio.run(scenario())


@pytest.mark.parametrize("handler_error", [False, True])
def test_completed_handlers_are_reaped_and_exceptions_observed(daemon_runtime, handler_error):
    async def scenario():
        async with daemon_runtime(handler_error=handler_error) as runtime:
            errors = []
            loop = asyncio.get_running_loop()
            previous = loop.get_exception_handler()
            loop.set_exception_handler(lambda _loop, context: errors.append(context))
            try:
                for _ in range(3):
                    reader, _writer = await runtime.open_peer({"operation": "job_status"})
                    assert json.loads(await asyncio.wait_for(reader.readline(), 2))["ok"] is True
                    assert await asyncio.wait_for(reader.read(), 2) == b""
                for _ in range(4):
                    await asyncio.sleep(0)
                gc.collect()
                assert len(runtime.handler_refs) == 3
                assert all(reference() is None for reference in runtime.handler_refs)
                assert not errors and not runtime.daemon.done()
                await stop_daemon(runtime, "cancel")
                assert asyncio.all_tasks() == runtime.tasks_before
            finally:
                loop.set_exception_handler(previous)

    asyncio.run(scenario())


def test_shutdown_preserves_issued_receipt_cleanup_before_inbox_and_client(daemon_runtime, monkeypatch, tmp_path):
    async def scenario():
        entered = asyncio.Event()
        async with daemon_runtime() as runtime:
            runtime.receipt_required = True
            receipt = tmp_path / "state" / "synthetic-receipt.json"

            async def operation(*_args, **_kwargs):
                runtime.events.append("effect_issued")
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # A cooperative already-issued fake effect has its known
                    # response and saves the receipt during executor cleanup.
                    runtime.events.append("effect_cleanup")
                    assert runtime.inbox.status()["open_events"] == 0
                    receipt.write_text('{"verified":true}\n')
                    runtime.receipt_saved.set()
                return {"ok": True, "receipt_key": "synthetic-receipt"}

            def handler_cancelled():
                assert not runtime.receipt_saved.is_set()
                assert bridge._effect_executor().active

            runtime.on_handler_cancel = handler_cancelled
            monkeypatch.setattr(bridge, "_handle_operation", operation)
            reader, writer = await runtime.open_peer({"operation": "send", "mode": "apply", "peer": "42"})
            await asyncio.wait_for(entered.wait(), 2)
            await stop_daemon(runtime, "cancel")
            assert not writer.is_closing() and await asyncio.wait_for(reader.read(), 2) == b""
            assert json.loads(receipt.read_text()) == {"verified": True}
            positions = [
                runtime.events.index(name)
                for name in ("handler_cancelled", "effect_cleanup", "inbox_close", "client_disconnect")
            ]
            assert positions == sorted(positions)
            assert not bridge._effect_executor().active
            assert asyncio.all_tasks() == runtime.tasks_before

    asyncio.run(scenario())
