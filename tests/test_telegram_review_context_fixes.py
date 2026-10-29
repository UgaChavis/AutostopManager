"""Deleted/edited Telegram context must survive partial failures without stale sends."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError
from test_telegram_bridge_durable import Client, _job, _message, _readback, _runtime, _send


@pytest.fixture(autouse=True)
def synthetic_transport(monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)


def deletion_callback(monitor):
    handlers = []
    registration = SimpleNamespace(add_event_handler=lambda handler, event: handlers.append((event, handler)))
    events = SimpleNamespace(MessageEdited=lambda: "edit", MessageDeleted=lambda: "delete")
    bridge._install_context_handlers(registration, events, monitor)
    return next(handler for name, handler in handlers if name == "delete")


@pytest.mark.parametrize("outgoing", [False, True])
def test_delete_storage_rollback_keeps_old_ram_version_and_reread_revokes_draft(tmp_path, outgoing):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        rows = [_message(100), _message(101, out=outgoing), _message(102)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)
        client = Client(rows)
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="old answer")
            revision = monitor.inbox._peer_revision(42)
            fingerprint = monitor.context_messages[42][101]
            denied = False

            def deny_once(action, table, column, *_args):
                nonlocal denied
                if not denied and action == sqlite3.SQLITE_UPDATE and table == "peers" and column == "context_revision":
                    denied = True
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            monitor.inbox._db.set_authorizer(deny_once)
            with pytest.raises(InboxError, match=r"^inbox_storage_failed$"):
                monitor.deleted([101], 42)
            monitor.inbox._db.set_authorizer(None)
            assert denied
            assert monitor.inbox._peer_revision(42) == revision
            assert monitor.context_messages[42][101] == fingerprint
            del client.messages[101]
            with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
                await _send(client, config, monitor, job, kind="result", text="old answer", mode="apply", dry=draft)
            assert monitor.inbox._peer_revision(42) == revision + 1
            assert monitor.context_messages[42][101] is None
            assert client.sent == 0
            assert not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
            monitor.deleted([101], 42)
            assert monitor.inbox._peer_revision(42) == revision + 1
        finally:
            monitor.inbox._db.set_authorizer(None)
            monitor.inbox.close()

    asyncio.run(scenario())


def test_native_known_channel_deletion_does_not_touch_private_context(tmp_path):
    telegram_events = pytest.importorskip("telethon.events")
    telegram_types = pytest.importorskip("telethon.tl.types")

    async def scenario():
        config, monitor = _runtime(tmp_path)
        row = _message(100)
        monitor.record(row)
        job = _job(monitor)
        client = Client([row])
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="answer")
            revision = monitor.inbox._peer_revision(42)
            fingerprint = monitor.context_messages[42][100]
            event = telegram_events.MessageDeleted.build(
                telegram_types.UpdateDeleteChannelMessages(channel_id=9876543210, messages=[100], pts=1, pts_count=1)
            )
            assert event.chat_id == -1009876543210
            await deletion_callback(monitor)(event)
            assert monitor.inbox._peer_revision(42) == revision
            assert monitor.context_messages[42][100] == fingerprint
            result = await _send(client, config, monitor, job, kind="result", text="answer", mode="apply", dry=draft)
            assert result["verified"] is True and client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_native_unknown_peer_deletion_still_invalidates_known_private_locator(tmp_path):
    telegram_events = pytest.importorskip("telethon.events")
    telegram_types = pytest.importorskip("telethon.tl.types")

    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(100), _message(101), _message(102)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)
        client = Client(rows)
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="answer")
            revision = monitor.inbox._peer_revision(42)
            event = telegram_events.MessageDeleted.build(
                telegram_types.UpdateDeleteMessages(messages=[101], pts=1, pts_count=1)
            )
            assert event.chat_id is None
            await deletion_callback(monitor)(event)
            assert monitor.inbox._peer_revision(42) == revision + 1
            assert monitor.context_messages[42][101] is None
            with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
                await _send(client, config, monitor, job, kind="result", text="answer", mode="apply", dry=draft)
            assert client.sent == 0
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("prepared", [False, True], ids=["first-dry-run", "renewed-dry-run"])
@pytest.mark.parametrize("change", ["text", "photo", "document"])
def test_changed_anchor_requires_context_reread_and_refresh_before_fresh_dry_run(tmp_path, prepared, change):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100, text="old request")
        identity = SimpleNamespace(id=1001, dc_id=2, attributes=[], file_reference=b"old", access_hash=123)
        if change != "text":
            source.media = SimpleNamespace()
            setattr(source, change, identity)
        event_id = monitor.record(source)
        job = _job(monitor)
        client = Client([source])
        try:
            old_draft = (
                await _send(client, config, monitor, job, kind="result", text="old answer") if prepared else None
            )
            revision = monitor.inbox._peer_revision(42)
            if change == "text":
                source.message = "revised client request"
            else:
                identity.id = 1002
            if old_draft is not None:
                with pytest.raises(bridge.BridgeError, match=r"^contract_reply_source_changed$"):
                    await _send(
                        client, config, monitor, job, kind="result", text="old answer", mode="apply", dry=old_draft
                    )
            with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
                await _send(client, config, monitor, job, kind="result", text="old answer")
            assert monitor.inbox._peer_revision(42) == revision + 1
            assert client.sent == 0
            assert all(not effect["attempted"] for effect in monitor.inbox.read_job(job["job_id"])["effects"])
            reread = await bridge._handle_monitor_read(client, monitor, event_id)
            assert reread["message"]["text"] == source.message
            with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
                await _send(client, config, monitor, job, kind="result", text="revised answer")
            refreshed = monitor.inbox.refresh_job(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], event_ids=job["event_ids"]
            )
            fresh = await _send(client, config, monitor, refreshed, kind="result", text="revised answer")
            result = await _send(
                client, config, monitor, refreshed, kind="result", text="revised answer", mode="apply", dry=fresh
            )
            assert result["verified"] is True and client.sent == 1
            assert monitor.inbox.read_effect(fresh["idempotency_key"])["context_revision"] == revision + 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_anchor_dry_run_invalidation_does_not_block_verified_receipt_readback(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100, text="old request")
        monitor.record(source)
        job = _job(monitor)
        client = Client([source])
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="delivered answer")
            sent = await _send(
                client, config, monitor, job, kind="result", text="delivered answer", mode="apply", dry=draft
            )
            source.message = "corrected after delivery"
            with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
                await _send(client, config, monitor, job, kind="result", text="delivered answer")
            proof = await _readback(client, config, monitor, job, sent["receipt_key"])
            assert proof["verified"] is True
            assert proof["receipt_key"] == sent["receipt_key"]
            assert client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())
