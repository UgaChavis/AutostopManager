"""History pages keep an exclusive peer-bound cursor under gaps and new arrivals."""

from __future__ import annotations

import asyncio

import pytest

from autostop_manager import telegram_bridge as bridge
from test_telegram_bridge_durable import _message, _runtime
from test_telegram_point_fixes import HistoryClient


@pytest.fixture
def peer(monkeypatch):
    async def resolve(_client, value):
        assert value == "42"
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)


async def read(client, config, before=None):
    request = {"operation": "read", "peer": "42", "limit": 100}
    if before is not None:
        request["before_message_id"] = before
    return await bridge._dispatch_operation(client, config, request)


@pytest.mark.parametrize("count,has_more", [(0, False), (100, False), (101, True)])
def test_empty_exact_page_and_one_extra_message(tmp_path, peer, count, has_more):
    config, monitor = _runtime(tmp_path)
    try:
        client = HistoryClient([_message(i) for i in range(1, count + 1)])
        page = asyncio.run(read(client, config))
        assert len(page["messages"]) == min(count, 100)
        assert page["has_more"] is has_more
        assert page["next_before_message_id"] == (2 if has_more else None)
        if has_more:
            tail = asyncio.run(read(client, config, page["next_before_message_id"]))
            assert [row["id"] for row in tail["messages"]] == [1]
            assert tail["has_more"] is False
    finally:
        monitor.inbox.close()


def test_deleted_cursor_id_gaps_and_new_arrival_do_not_duplicate_or_skip_old_rows(tmp_path, peer):
    config, monitor = _runtime(tmp_path)
    try:
        client = HistoryClient([_message(i) for i in range(2, 303, 2)])
        first = asyncio.run(read(client, config))
        cursor = first["next_before_message_id"]
        assert cursor == 104
        del client.messages[cursor]
        client.messages[304] = _message(304)
        second = asyncio.run(read(client, config, cursor))
        repeated = asyncio.run(read(client, config, cursor))
        assert [row["id"] for row in second["messages"]] == list(range(2, 104, 2))
        assert repeated == second
        assert not {row["id"] for row in first["messages"]} & {row["id"] for row in second["messages"]}
        assert second["has_more"] is False
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("fault", ["other_peer", "duplicate", "nonexclusive", "invalid_id"])
def test_bad_transport_page_is_rejected_before_any_history_is_returned(tmp_path, peer, fault):
    config, monitor = _runtime(tmp_path)

    class BadPage:
        async def get_messages(self, _entity, **_kwargs):
            if fault == "other_peer":
                return [_message(4, peer=43)]
            if fault == "duplicate":
                return [_message(4), _message(4)]
            if fault == "nonexclusive":
                return [_message(5)]
            return [_message(0)]

    try:
        with pytest.raises(bridge.BridgeError, match="history_peer_invalid"):
            asyncio.run(read(BadPage(), config, 5))
    finally:
        monitor.inbox.close()
