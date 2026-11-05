from __future__ import annotations

import asyncio
import errno
import json
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


EVENT = SimpleNamespace(peer_id=42, message_id=100)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (PermissionError("private detail"), "telegram_permission_denied"),
        (TimeoutError("private detail"), "telegram_transport_error"),
        (ConnectionResetError("private detail"), "telegram_transport_error"),
        (OSError(errno.ENETUNREACH, "private detail"), "telegram_transport_error"),
        (OSError(errno.ENOSPC, "private detail"), "telegram_operation_failed"),
        (OSError("private detail"), "telegram_operation_failed"),
        (RuntimeError("private detail"), "telegram_operation_failed"),
    ],
)
def test_global_lookup_error_stops_without_dialog_fallback(failure, expected):
    class Client:
        global_reads = 0
        dialog_scans = 0

        async def get_messages(self, entity, *, ids):
            assert entity is None and ids == 100
            self.global_reads += 1
            raise failure

        async def iter_dialogs(self, *, limit):
            self.dialog_scans += 1
            yield None

    client = Client()
    with pytest.raises(bridge.BridgeError) as error:
        asyncio.run(bridge._load_inbound_monitor_context_messages(client, [EVENT]))
    assert error.value.code == expected
    assert str(error.value) == expected
    assert error.value.__cause__ is failure
    assert (client.global_reads, client.dialog_scans) == (1, 0)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (PermissionError("private detail"), "telegram_permission_denied"),
        (ConnectionResetError("private detail"), "telegram_transport_error"),
        (OSError(errno.ENOSPC, "private detail"), "telegram_operation_failed"),
        (RuntimeError("private detail"), "telegram_operation_failed"),
    ],
)
def test_fallback_failure_is_classified_without_exposing_details(failure, expected):
    class Client:
        global_reads = 0
        dialog_scans = 0

        async def get_messages(self, entity, *, ids):
            self.global_reads += 1
            assert entity is None and ids == 100
            return

        async def iter_dialogs(self, *, limit):
            assert limit == bridge.MAX_INBOUND_MONITOR_FALLBACK_DIALOGS
            self.dialog_scans += 1
            raise failure
            yield

    client = Client()
    with pytest.raises(bridge.BridgeError) as error:
        asyncio.run(bridge._load_inbound_monitor_context_messages(client, [EVENT]))
    assert error.value.code == expected
    assert str(error.value) == expected
    assert error.value.__cause__ is failure
    assert (client.global_reads, client.dialog_scans) == (1, 1)


@pytest.mark.parametrize("error_name", ["AuthKeyUnregisteredError", "FloodWaitError"])
def test_telethon_auth_and_quota_errors_do_not_trigger_fallback(error_name):
    errors = pytest.importorskip("telethon.errors")
    failure = getattr(errors, error_name)(request=None)

    class Client:
        calls = 0

        async def get_messages(self, entity, *, ids):
            self.calls += 1
            raise failure

        async def iter_dialogs(self, *, limit):
            pytest.fail("provider auth or quota failure must not trigger a dialog scan")
            yield

    client = Client()
    with pytest.raises(bridge.BridgeError) as error:
        asyncio.run(bridge._load_inbound_monitor_context_messages(client, [EVENT]))
    assert error.value.code == "telegram_operation_failed"
    assert str(error.value) == "telegram_operation_failed"
    assert error.value.__cause__ is failure
    assert client.calls == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (PermissionError("private detail"), "telegram_permission_denied"),
        (ConnectionResetError("private detail"), "telegram_transport_error"),
        (RuntimeError("private detail"), "telegram_operation_failed"),
    ],
)
def test_exact_peer_read_failure_during_fallback_is_classified(failure, expected):
    entity = object()

    class Client:
        reads = 0
        scans = 0

        async def get_messages(self, actual_entity, *, ids):
            self.reads += 1
            assert ids == 100
            if actual_entity is None:
                return
            assert actual_entity is entity
            raise failure

        async def iter_dialogs(self, *, limit):
            self.scans += 1
            assert limit == bridge.MAX_INBOUND_MONITOR_FALLBACK_DIALOGS
            yield SimpleNamespace(id=42, entity=entity)

    client = Client()
    with pytest.raises(bridge.BridgeError) as error:
        asyncio.run(bridge._load_inbound_monitor_context_messages(client, [EVENT]))
    assert error.value.code == expected
    assert str(error.value) == expected
    assert error.value.__cause__ is failure
    assert (client.reads, client.scans) == (2, 1)


def test_cancelled_lookup_does_not_attempt_fallback():
    class Client:
        async def get_messages(self, entity, *, ids):
            raise asyncio.CancelledError

        async def iter_dialogs(self, *, limit):
            pytest.fail("cancellation must not trigger another operation")
            yield

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridge._load_inbound_monitor_context_messages(Client(), [EVENT]))


@pytest.mark.parametrize("operation", ["monitor_read", "monitor_target"])
def test_rpc_does_not_leak_details_or_retry_on_permission_error(tmp_path, operation):
    class Client:
        calls = 0

        async def get_messages(self, entity, *, ids):
            self.calls += 1
            raise PermissionError("private detail")

        async def iter_dialogs(self, *, limit):
            pytest.fail("permission error must not start a dialog scan")
            yield

    class Monitor:
        def resolve(self, event_id):
            assert event_id == "inbound-1"
            return EVENT

        def require_open(self, event_id):
            return self.resolve(event_id)

    client = Client()

    async def scenario():
        path = str(tmp_path / "bridge.sock")
        server = await asyncio.start_unix_server(
            lambda reader, writer: bridge._serve_client(client, None, reader, writer, inbound_monitor=Monitor()),
            path=path,
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(json.dumps({"operation": operation, "event_id": "inbound-1"}).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            writer.close()
            await writer.wait_closed()
            assert json.loads(raw) == {"ok": False, "error": "telegram_permission_denied"}
            assert b"private detail" not in raw
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    assert client.calls == 1
