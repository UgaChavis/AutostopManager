import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from autostop_manager.telegram_bridge import (
    DurableInboundMonitor,
    _capture_incoming_event,
    _handle_monitor_operation,
    _incoming_event_kind,
    _incoming_sender_kind,
    _reconcile_peer_history,
)
from autostop_manager.telegram_inbox import TelegramInbox


class User:
    def __init__(self, peer, *, bot=None, minimum=False):
        self.id = peer
        self.bot = bot
        self.min = minimum


def test_sender_classification_requires_exact_full_telegram_user():
    message = SimpleNamespace(chat_id=12, sender_id=12)
    assert _incoming_sender_kind(message, User(12, bot=True)) == "bot"
    assert _incoming_sender_kind(message, User(12, bot=None)) == "client"
    assert _incoming_sender_kind(message, User(12, bot=False)) == "client"
    assert _incoming_sender_kind(message, User(12, minimum=True)) == "unknown"
    assert _incoming_sender_kind(message, User(13, bot=True)) == "unknown"
    assert _incoming_sender_kind(SimpleNamespace(chat_id=12, sender_id=13), User(12)) == "unknown"
    assert _incoming_sender_kind(message, SimpleNamespace(id=12, bot=True)) == "unknown"


def test_callback_sender_resolution_and_failure():
    class Event:
        chat_id = sender_id = 12
        sender = None

        async def get_sender(self):
            return User(12, bot=True)

    class Failed(Event):
        async def get_sender(self):
            raise OSError("unavailable")

    assert asyncio.run(_incoming_event_kind(Event())) == "bot"
    assert asyncio.run(_incoming_event_kind(Failed())) == "unknown"


def test_callback_routes_verified_bot_and_person_to_different_queues(tmp_path):
    with TelegramInbox(tmp_path / "inbox" / "events.sqlite3") as inbox:
        inbox.duty_set(True)
        monitor = DurableInboundMonitor(inbox, enabled=True)
        bot = SimpleNamespace(id=1, chat_id=12, sender_id=12, is_private=True, out=False, sender=User(12, bot=True))
        person = SimpleNamespace(id=2, chat_id=13, sender_id=13, is_private=True, out=False, sender=User(13))
        unknown = SimpleNamespace(id=3, chat_id=14, sender_id=14, is_private=True, out=False, sender=None)
        for message in (bot, person, unknown):
            asyncio.run(_capture_incoming_event(monitor, message))
        assert [item["state"] for item in inbox.events()] == ["bot_pending", "open", "identity_pending"]
        job = inbox.claim(instruction_root="/opt/test-runtime", instruction_revision="a" * 40)
        assert job["event_ids"] == [inbox.events()[1]["event_id"]]


def test_bot_read_acknowledge_and_unknown_reclassify(tmp_path):
    with TelegramInbox(tmp_path / "inbox" / "events.sqlite3") as inbox:
        inbox.duty_set(True)
        monitor = DurableInboundMonitor(inbox, enabled=True)
        bot = SimpleNamespace(
            id=1,
            chat_id=12,
            sender_id=12,
            is_private=True,
            out=False,
            sender=User(12, bot=True),
            message="notice",
            media=None,
            date=None,
        )
        unknown = SimpleNamespace(
            id=2,
            chat_id=13,
            sender_id=13,
            is_private=True,
            out=False,
            sender=None,
            message="question",
            media=None,
            date=None,
        )
        asyncio.run(_capture_incoming_event(monitor, bot))
        asyncio.run(_capture_incoming_event(monitor, unknown))
        bot_id, unknown_id = (item["event_id"] for item in inbox.events())

        class Client:
            async def get_messages(self, peer, *, ids):
                return {1: bot, 2: unknown}[ids]

        client = Client()
        read = asyncio.run(
            _handle_monitor_operation(client, None, {"operation": "monitor_read", "event_id": bot_id}, monitor)
        )
        assert read["message"]["text"] == "notice"
        marked = asyncio.run(
            _handle_monitor_operation(
                client,
                None,
                {"operation": "monitor_mark", "event_id": bot_id, "disposition": "no_reply_needed"},
                monitor,
            )
        )
        assert marked["event"]["state"] == "no_reply_needed"
        unknown.sender = User(13)
        resolved = asyncio.run(
            _handle_monitor_operation(
                client, None, {"operation": "monitor_reclassify", "event_id": unknown_id}, monitor
            )
        )
        assert resolved["state"] == "open"


def test_reconnect_recovery_keeps_bot_out_of_client_queue(tmp_path):
    with TelegramInbox(tmp_path / "inbox" / "events.sqlite3") as inbox:
        inbox.duty_set(True, at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        monitor = DurableInboundMonitor(inbox, enabled=True)
        monitor.reconciliation_transport_hook = lambda: True
        message = SimpleNamespace(
            id=4,
            chat_id=12,
            sender_id=12,
            is_private=True,
            out=False,
            date=datetime.now(UTC),
            message="notice",
            media=None,
        )

        class Client:
            async def iter_messages(self, entity, **kwargs):
                yield message

        consumed, complete, blocked = asyncio.run(_reconcile_peer_history(Client(), User(12, bot=True), 12, monitor, 5))
        assert (consumed, complete, blocked) == (1, True, False)
        assert inbox.events()[0]["state"] == "bot_pending"
        assert inbox.claim(instruction_root="/opt/test-runtime", instruction_revision="a" * 40) is None
