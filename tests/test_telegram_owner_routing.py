"""Owner delivery over synthetic refs, isolated ledgers, and no live Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


ROOT = "/test/owner-snapshot"
REVISION = "a" * 40
OWNER = 42


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now]
    inbox = TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=lambda: clock[0])
    inbox.configure_instructions(ROOT, REVISION)
    inbox.duty_set(True, at=(now - timedelta(hours=1)).isoformat())
    inbox.configure_owner_route(OWNER)
    clock[0] += timedelta(seconds=1)
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    config = bridge.TelegramConfig(123456, "a" * 32, state_dir=tmp_path, socket_path=tmp_path / "bridge.sock")
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config, **_kwargs: OWNER)
    yield inbox, monitor, config, clock
    inbox.close()


def claim(inbox):
    return inbox.claim(instruction_root=ROOT, instruction_revision=REVISION, exclude_peer_id=OWNER)


def test_owner_delivery_does_not_acquire_client_slot_and_receipt_is_idempotent(runtime):
    inbox, _, _, _ = runtime
    owner = inbox.record(OWNER, 1)
    client = inbox.record(43, 2)
    job = claim(inbox)
    assert job["event_ids"] == [client]
    receipt = inbox.route_owner_event(
        event_id=owner,
        owner_peer_id=OWNER,
        receipt_id="owner-receipt-1",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    assert receipt["outcome"] == "routed"
    assert inbox.read_job(receipt["job_id"])["outcome"] == "routed"
    assert inbox.execution_slot()["job_id"] == job["job_id"]
    assert inbox.owner_pending(OWNER) == []
    assert inbox.owner_routes_unfinished() == [{"event_id": owner, "receipt_id": "owner-receipt-1"}]
    assert (
        inbox.route_owner_event(
            event_id=owner,
            owner_peer_id=OWNER,
            receipt_id="owner-receipt-1",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
        == receipt
    )
    with pytest.raises(InboxError, match="inbox_owner_receipt_conflict"):
        inbox.route_owner_event(
            event_id=owner,
            owner_peer_id=OWNER,
            receipt_id="owner-receipt-2",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    assert inbox.complete_owner_route(owner, "owner-receipt-1") == receipt
    assert inbox.owner_routes_unfinished() == []
    with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
        inbox.require_owner_source(client, OWNER)


def test_startup_cutover_holds_old_tests_and_survives_restart(tmp_path):
    path = tmp_path / "inbox" / "inbound.sqlite3"
    clock = [datetime(2026, 10, 27, 12, tzinfo=UTC)]
    with TelegramInbox(path, clock=lambda: clock[0]) as inbox:
        inbox.configure_instructions(ROOT, REVISION)
        inbox.duty_set(True, at=(clock[0] - timedelta(hours=1)).isoformat())
        old = inbox.record(OWNER, 1)
        clock[0] += timedelta(seconds=1)
        inbox.configure_owner_route(OWNER)
        generation = inbox.generation
        inbox.configure_owner_route(OWNER)
        assert inbox.generation == generation
        assert inbox.owner_pending(OWNER) == []
        with pytest.raises(InboxError, match="inbox_owner_event_not_pending"):
            inbox.require_owner_source(old, OWNER)
        old_job = inbox.active_job_page()["jobs"][0]
        assert old_job["reason_code"] == "owner_role_transition_review_required"
        assert inbox.execution_slot() is None
        assert inbox.job_status()["compatibility_hold"] is False
        clock[0] += timedelta(seconds=1)
        fresh = inbox.record(OWNER, 2)
        client = inbox.record(43, 3)
        assert claim(inbox)["event_ids"] == [client]
    with TelegramInbox(path, clock=lambda: clock[0]) as restored:
        restored.configure_instructions(ROOT, REVISION)
        generation = restored.generation
        restored.configure_owner_route(OWNER)
        assert restored.generation == generation
        assert [event["event_id"] for event in restored.owner_pending(OWNER)] == [fresh]
        # A historical update recovered only after restart keeps its original authority.
        recovered = restored.record(OWNER, 4, (clock[0] - timedelta(seconds=3)).isoformat())
        with pytest.raises(InboxError, match="inbox_owner_event_not_pending"):
            restored.require_owner_source(recovered, OWNER)
        receipt = restored.route_owner_event(
            event_id=fresh,
            owner_peer_id=OWNER,
            receipt_id="owner-restart",
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    with TelegramInbox(path, clock=lambda: clock[0]) as restored:
        assert restored.owner_route_receipt(fresh) == receipt
        assert restored.require_owner_source(fresh, OWNER).message_id == 2


def test_owner_source_exact_read_after_routing_preserves_reply_reference(runtime, monkeypatch):
    inbox, monitor, config, _ = runtime
    event_id = inbox.record(OWNER, 25)
    client_id = inbox.record(43, 26)
    inbox.route_owner_event(
        event_id=event_id,
        owner_peer_id=OWNER,
        receipt_id="owner-receipt",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    source = SimpleNamespace(id=25, message="Разрешаю именно этот выпуск", reply_to_msg_id=24, media=None)
    reads = []

    async def load(_client, events):
        reads.extend((event.peer_id, event.message_id) for event in events)
        return [source]

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    response = asyncio.run(
        bridge._handle_operation(
            object(), config, {"operation": "owner_event_read", "event_id": event_id}, inbound_monitor=monitor
        )
    )
    assert response["role"] == "owner"
    assert response["source_message_id"] == 25 and response["reply_to_message_id"] == 24
    assert response["routing_receipt"]["receipt_id"] == "owner-receipt"
    assert reads == [(OWNER, 25)]
    with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
        asyncio.run(
            bridge._handle_operation(
                object(), config, {"operation": "owner_event_read", "event_id": client_id}, inbound_monitor=monitor
            )
        )
    assert len(reads) == 1


@pytest.mark.parametrize(
    "operation", ["owner_event_read", "owner_event_voice_stage", "job_owner_pending", "job_client_control"]
)
def test_owner_read_and_route_rpc_require_root_before_source_access(runtime, monkeypatch, operation):
    _, monitor, config, _ = runtime
    access = []

    def reject(_writer):
        access.append("root-check")
        raise bridge.BridgeError("root_required")

    monkeypatch.setattr(bridge, "_require_root_client", reject)

    class Reader:
        async def readline(self):
            return ('{"operation":"' + operation + '","event_id":"inbound-1"}\n').encode()

    class Writer:
        def __init__(self):
            self.body = b""

        def write(self, value):
            self.body += value

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    writer = Writer()
    asyncio.run(bridge._serve_client_request(object(), config, Reader(), writer, inbound_monitor=monitor))
    assert access == ["root-check"]
    assert b"root_required" in writer.body


def test_client_only_cutover_preserves_old_owner_receipts_and_routes_new_as_clients(runtime, monkeypatch):
    inbox, monitor, config, clock = runtime
    routed = inbox.record(OWNER, 1)
    receipt = inbox.route_owner_event(
        event_id=routed,
        owner_peer_id=OWNER,
        receipt_id="historical-owner",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    pending = inbox.record(OWNER, 2)
    inbox.configure_client_only(OWNER)
    assert inbox.owner_route_receipt(routed) == receipt
    assert inbox.require_owner_source(routed, OWNER).message_id == 1
    assert inbox.owner_pending(OWNER) == []
    assert inbox.event_state(pending) == "retired"
    clock[0] += timedelta(seconds=1)
    fresh = inbox.record(OWNER, 3)

    async def resolve(_client, peer):
        return int(peer), {"id": int(peer), "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def load(_client, _events):
        return [SimpleNamespace(id=3, chat_id=OWNER, out=False, is_private=True)]

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    target = asyncio.run(bridge._handle_monitor_target(object(), config, monitor, fresh))
    assert target["role"] == "client" and target["target"]["id"] == OWNER
    with pytest.raises(InboxError, match="inbox_owner_route_retired"):
        inbox.require_owner_source(fresh, OWNER)
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION)["event_ids"] == [fresh]


def test_routed_owner_voice_uses_exact_source_and_cleanup_without_client_lease(runtime, monkeypatch):
    inbox, monitor, config, _ = runtime
    owner = inbox.record(OWNER, 1)
    client = inbox.record(43, 2)
    job = claim(inbox)
    inbox.route_owner_event(
        event_id=owner,
        owner_peer_id=OWNER,
        receipt_id="owner-voice",
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    audio = b"OggSsynthetic-voice"
    attribute = type("DocumentAttributeAudio", (), {"voice": True, "duration": 8})()
    voice = SimpleNamespace(
        id=1,
        media=object(),
        file=SimpleNamespace(mime_type="audio/ogg", name="", size=len(audio)),
        document=SimpleNamespace(attributes=[attribute]),
    )

    async def load(_client, events):
        assert [(event.peer_id, event.message_id) for event in events] == [(OWNER, 1)]
        return [voice]

    class Client:
        async def download_media(self, message, *, file):
            assert message is voice and file is bytes
            return audio

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: None)

    async def scenario():
        result = await bridge._handle_operation(
            Client(), config, {"operation": "owner_event_voice_stage", "event_id": owner}, inbound_monitor=monitor
        )
        handle = result["media_handle"]
        path = monitor.voice_stage(handle).path
        assert path.read_bytes() == audio
        discarded = await bridge._handle_operation(
            Client(), config, {"operation": "monitor_voice_discard", "media_handle": handle}, inbound_monitor=monitor
        )
        assert discarded["cleanup_verified"] is True and not path.exists()
        with pytest.raises(InboxError, match="inbox_owner_target_mismatch"):
            await bridge._handle_operation(
                Client(), config, {"operation": "owner_event_voice_stage", "event_id": client}, inbound_monitor=monitor
            )
        assert inbox.execution_slot()["job_id"] == job["job_id"]

    asyncio.run(scenario())
