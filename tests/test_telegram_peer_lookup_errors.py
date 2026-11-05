from __future__ import annotations

import asyncio
import errno
import json
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("not in cache"), "peer_not_found"),
        (TimeoutError("private detail"), "telegram_transport_error"),
        (ConnectionResetError("private detail"), "telegram_transport_error"),
        (OSError(errno.ENETUNREACH, "private detail"), "telegram_transport_error"),
        (OSError("private detail"), "telegram_operation_failed"),
        (OSError(errno.ENOSPC, "private detail"), "telegram_operation_failed"),
        (PermissionError("private detail"), "telegram_operation_failed"),
        (RuntimeError("private detail"), "telegram_operation_failed"),
    ],
)
def test_peer_lookup_classifies_failures_without_provider_details(monkeypatch, failure, expected):
    class Client:
        async def get_entity(self, peer):
            assert peer == 42
            raise failure

    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, SimpleNamespace(), None))
    with pytest.raises(bridge.BridgeError) as error:
        asyncio.run(bridge._resolve_peer(Client(), "42"))
    assert error.value.code == expected
    assert str(error.value) == expected
    assert error.value.__cause__ is failure


def test_peer_lookup_preserves_cancellation(monkeypatch):
    class Client:
        async def get_entity(self, peer):
            raise asyncio.CancelledError

    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, SimpleNamespace(), None))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridge._resolve_peer(Client(), "42"))


def test_peer_lookup_returns_the_same_successful_private_peer(monkeypatch):
    class User:
        id = 42
        first_name = "Synthetic"
        bot = False

    entity = User()

    class Client:
        async def get_entity(self, peer):
            assert peer == 42
            return entity

    utils = SimpleNamespace(get_peer_id=lambda value: value.id)
    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, utils, None))
    resolved, target = asyncio.run(bridge._resolve_peer(Client(), "42"))
    assert resolved is entity
    assert target["id"] == 42
    assert target["kind"] == "private"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("private detail"), "telegram_transport_error"),
        (PermissionError("private detail"), "telegram_operation_failed"),
        (ValueError("private detail"), "peer_not_found"),
    ],
)
def test_read_rpc_returns_only_classification_and_does_not_retry(tmp_path, monkeypatch, failure, expected):
    class Client:
        calls = 0

        async def get_entity(self, peer):
            self.calls += 1
            raise failure

    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, SimpleNamespace(), None))
    client = Client()

    async def scenario():
        path = str(tmp_path / "bridge.sock")
        server = await asyncio.start_unix_server(
            lambda reader, writer: bridge._serve_client(client, None, reader, writer), path=path
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b'{"operation":"read","peer":"42"}\n')
            await writer.drain()
            raw = await reader.readline()
            writer.close()
            await writer.wait_closed()
            assert json.loads(raw) == {"ok": False, "error": expected}
            assert b"private detail" not in raw
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    assert client.calls == 1


def test_request_budget_timeout_remains_distinct(tmp_path, monkeypatch):
    class Client:
        calls = 0

        async def get_entity(self, peer):
            self.calls += 1
            await asyncio.sleep(1)

    monkeypatch.setattr(bridge, "_load_telethon", lambda: (None, SimpleNamespace(), None))
    monkeypatch.setattr(bridge, "_request_budget", lambda *_args: 0.01)
    client = Client()

    async def scenario():
        path = str(tmp_path / "bridge.sock")
        server = await asyncio.start_unix_server(
            lambda reader, writer: bridge._serve_client(client, None, reader, writer), path=path
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b'{"operation":"read","peer":"42"}\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert response == {"ok": False, "error": "bridge_request_timeout"}
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    assert client.calls == 1
