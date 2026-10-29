from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError
from test_telegram_bridge_durable import Client, MediaClient, _job, _media_request, _message, _readback, _runtime, _send


@pytest.fixture(autouse=True)
def synthetic_transport(monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)


@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_non_anchor_changes_revoke_draft_without_update_callback(tmp_path, outgoing, change):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        rows = [_message(100), _message(101, out=outgoing), _message(102)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)
        client = Client(rows)
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="old draft")
            revision = monitor.inbox._peer_revision(42)
            if change == "edit":
                rows[1].message = "corrected context"
                rows[1].edit_date = datetime.now(UTC)
            else:
                del client.messages[101]
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await _send(client, config, monitor, job, kind="result", text="old draft", mode="apply", dry=draft)
            assert client.sent == 0
            assert monitor.inbox._peer_revision(42) > revision
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_final_revalidation_catches_change_during_anchor_read(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(100), _message(101), _message(102)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)

        class EditingClient(Client):
            edit_on_anchor = False

            async def get_messages(self, entity, *, ids=None, limit=None):
                result = await super().get_messages(entity, ids=ids, limit=limit)
                if ids == 100 and self.edit_on_anchor:
                    self.messages[101].message = "changed after prepare"
                return result

        client = EditingClient(rows)
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="draft")
            client.edit_on_anchor = True
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await _send(client, config, monitor, job, kind="result", text="draft", mode="apply", dry=draft)
            assert client.sent == 0
            assert not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
def test_new_inbound_during_final_exact_rpc_revokes_all_send_kinds(tmp_path, monkeypatch, operation):
    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(identifier) for identifier in range(100, 103)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)

        class ArrivingClient(MediaClient):
            armed = False
            saw_watermark = False

            async def get_messages(self, entity, *, ids=None, limit=None):
                result = await super().get_messages(entity, ids=ids, limit=limit)
                if self.armed and limit == 1:
                    self.saw_watermark = True
                if self.armed and self.saw_watermark and isinstance(ids, list):
                    self.messages[103] = _message(103, text="new correction without callback")
                    self.armed = False
                return result

        client = ArrivingClient(rows)
        payload = (
            {"operation": "send", "peer": "42", "text": "draft", "mode": "dry_run"}
            if operation == "send"
            else _media_request(config, operation)
        ) | {
            "job_id": job["job_id"],
            "lease": job["lease"],
            "revision": job["revision"],
            "kind": "result",
            "reply_to_message_id": 100,
        }
        try:
            draft = await bridge._handle_operation(client, config, payload, inbound_monitor=monitor)
            client.armed = True
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await bridge._handle_operation(
                    client,
                    config,
                    payload
                    | {
                        "mode": "apply",
                        "contract_token": draft["contract_token"],
                        "idempotency_key": draft["idempotency_key"],
                    },
                    inbound_monitor=monitor,
                )
            assert client.sent == 0
            assert monitor.inbox.resolve(monitor.events()[-1]["event_id"]).message_id == 103
            assert not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_restart_revokes_unattempted_draft_but_receipt_readback_survives_edit(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(100), _message(101)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)
        client = Client(rows)
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="draft")
            restarted = bridge.DurableInboundMonitor(monitor.inbox, enabled=True)
            with pytest.raises(bridge.BridgeError, match="context_snapshot_unavailable"):
                await _send(client, config, restarted, job, kind="result", text="draft", mode="apply", dry=draft)
            assert client.sent == 0
            sent = await _send(client, config, monitor, job, kind="result", text="draft", mode="apply", dry=draft)
            rows[1].message = "edited after delivery"
            monitor.remember_context(rows[1])
            receipt = await _readback(client, config, monitor, job, sent["receipt_key"])
            assert receipt["verified"] is True
            assert client.sent == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_edit_delete_context_callbacks_do_not_create_inbound_events(tmp_path):
    _config, monitor = _runtime(tmp_path)
    try:
        row = _message(100)
        monitor.record(row)
        revision = monitor.inbox._peer_revision(42)
        row.message = "edited"
        monitor.remember_context(row)
        monitor.deleted([100], 42)
        monitor.deleted([100], 42)
        assert len(monitor.events()) == 1
        assert monitor.inbox._peer_revision(42) == revision + 2
        assert "edited" not in monitor.inbox.path.read_bytes().decode(errors="ignore")
    finally:
        monitor.inbox.close()


def test_missing_reply_anchor_preserves_source_error(tmp_path):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = Client([source])
        try:
            draft = await _send(client, config, monitor, job, kind="result", text="draft")
            del client.messages[100]
            with pytest.raises(bridge.BridgeError, match=r"^reply_source_not_found$"):
                await _send(client, config, monitor, job, kind="result", text="draft", mode="apply", dry=draft)
            assert client.sent == 0
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


class HistoryClient(Client):
    def __init__(self, messages):
        super().__init__(messages)
        self.calls = []

    async def get_messages(self, entity, *, ids=None, limit=None, offset_id=0):
        self.calls.append((entity, ids, limit, offset_id))
        if ids is not None:
            return await super().get_messages(entity, ids=ids, limit=limit)
        rows = sorted(
            (row for row in self.messages.values() if row.chat_id == entity and (not offset_id or row.id < offset_id)),
            key=lambda row: row.id,
            reverse=True,
        )
        return rows[:limit]


@pytest.mark.parametrize("outgoing", [False, True])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_used_older_history_is_revalidated_outside_recent_and_covered_refs(tmp_path, monkeypatch, outgoing, change):
    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.self_peer_id = 900
        older = _message(1, out=outgoing, text="previous agreement")
        rows = [older, *[_message(identifier) for identifier in range(100, 110)]]
        for row in rows[1:]:
            monitor.record(row)
        job = _job(monitor)
        client = HistoryClient(rows)
        try:
            history = await bridge._dispatch_operation(
                client, config, {"operation": "read", "peer": "42", "limit": 100}, inbound_monitor=monitor
            )
            assert history["messages"][0]["id"] == 1
            # Human outgoing discovery changes revision, requiring normal refresh.
            if outgoing:
                job = monitor.inbox.refresh_job(
                    job_id=job["job_id"],
                    lease=job["lease"],
                    expected_revision=job["revision"],
                    event_ids=job["event_ids"],
                )
            draft = await _send(client, config, monitor, job, kind="result", text="draft")
            if change == "edit":
                older.message = "corrected agreement"
            else:
                del client.messages[1]
            with pytest.raises(InboxError, match="inbox_context_stale"):
                await _send(client, config, monitor, job, kind="result", text="draft", mode="apply", dry=draft)
            assert client.sent == 0
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_history_pages_reach_old_agreement_without_gaps_or_cross_peer(tmp_path, monkeypatch):
    async def resolve(_client, peer):
        assert peer == "42"
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(i, out=i % 2 == 0, text="old agreement" if i == 1 else "context") for i in range(1, 151)]
        client = HistoryClient([*rows, _message(151, peer=43)])
        try:
            first = await bridge._dispatch_operation(
                client, config, {"operation": "read", "peer": "42", "limit": 100}, inbound_monitor=monitor
            )
            second = await bridge._dispatch_operation(
                client,
                config,
                {"operation": "read", "peer": "42", "limit": 100, "before_message_id": first["next_before_message_id"]},
                inbound_monitor=monitor,
            )
            assert [row["id"] for row in first["messages"]] == list(range(51, 151))
            assert [row["id"] for row in second["messages"]] == list(range(1, 51))
            assert second["messages"][0]["text"] == "old agreement"
            assert first["has_more"] and first["next_before_message_id"] == 51
            assert second["has_more"] is False and second["next_before_message_id"] is None
            assert client.calls[-1][3] == 51
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cursor", [True, False, "51", 1.2, 0, -1])
def test_history_rejects_non_integer_positive_cursor_before_transport(tmp_path, cursor):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        try:
            with pytest.raises(bridge.BridgeError, match="history_cursor_invalid"):
                await bridge._dispatch_operation(
                    object(), config, {"operation": "read", "peer": "42", "before_message_id": cursor}
                )
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_history_cli_passes_cursor_to_rpc(monkeypatch, capsys):
    requests = []
    monkeypatch.setattr(bridge, "send_local_request", lambda _path, request: requests.append(request) or {"ok": True})
    assert (
        bridge.main(["--account", "work", "read", "--peer", "42", "--limit", "100", "--before-message-id", "51"]) == 0
    )
    assert requests == [{"operation": "read", "peer": "42", "limit": 100, "before_message_id": 51}]
    capsys.readouterr()


def test_native_ledger_is_read_once_per_packet_and_failure_stays_unresolved(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monitor.config = config
    monitor.self_peer_id = 900
    rows = [_message(i, out=True) for i in range(100, 120)]
    calls = []

    def ledger(_path):
        calls.append(True)
        return {"known": {"peer_id": 42, "message_id": 100}, "pending": {"peer_id": 42, "message_id": 0}}

    monkeypatch.setattr(bridge, "_load_idempotency", ledger)
    try:
        monitor.observe_packet(rows)
        assert len(calls) == 1
        context = {row["message_id"]: row["origin"] for row in monitor.inbox.outgoing_context(42)}
        assert context[100] == "agent" and context[101] == "unresolved"
        monitor.reconcile_outgoing(42)
        assert len(calls) == 2

        def broken(_path):
            raise bridge.BridgeError("idempotency_state_invalid")

        monkeypatch.setattr(bridge, "_load_idempotency", broken)
        monitor.observe_packet([_message(120, out=True)])
        assert (
            next(row for row in monitor.inbox.outgoing_context(42) if row["message_id"] == 120)["origin"]
            == "unresolved"
        )
    finally:
        monitor.inbox.close()


def test_history_reconciliation_uses_one_native_snapshot(tmp_path, monkeypatch):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        monitor.config, monitor.self_peer_id = config, 900
        rows = [_message(i, out=True) for i in range(100, 120)]
        calls = []

        def ledger(_path):
            calls.append(True)
            return {}

        monkeypatch.setattr(bridge, "_load_idempotency", ledger)
        try:
            consumed, complete, blocked = await bridge._reconcile_peer_history(Client(rows), 42, 42, monitor, 100)
            assert consumed == 20 and complete and not blocked
            assert len(calls) == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())
