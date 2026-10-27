"""Check reply target types before the work Telegram effect guard."""

import asyncio

import pytest

from autostop_manager import telegram_bridge
from autostop_manager.telegram_bridge import (
    BridgeError,
    DurableInboundMonitor,
    _prepare_job_send,
    validate_send_request,
)


@pytest.mark.parametrize("reply_id", [0.9, 1.9, 0.0, 1.0, [], {}, True, False])
def test_non_integer_reply_id_is_rejected_before_job_binding(reply_id):
    request = {"peer": "1", "text": "synthetic", "mode": "dry_run", "reply_to_message_id": reply_id}
    with pytest.raises(BridgeError, match="reply_to_message_id_invalid"):
        validate_send_request(request)


@pytest.mark.parametrize("reply_id", [0.9, 1.9, [], {}])
def test_work_send_rejects_invalid_reply_before_resolving_peer(monkeypatch, reply_id):
    async def denied(*_args, **_kwargs):
        raise AssertionError("invalid reply must not resolve a peer or reach Telegram")

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", denied)
    request = {
        "operation": "send",
        "peer": "1",
        "text": "synthetic",
        "mode": "dry_run",
        "reply_to_message_id": reply_id,
    }
    monitor = object.__new__(DurableInboundMonitor)
    with pytest.raises(BridgeError, match="reply_to_message_id_invalid"):
        asyncio.run(telegram_bridge._handle_operation(None, None, request, inbound_monitor=monitor))


@pytest.mark.parametrize("reply_id, expected", [(0, 0), (1, 1), ("1", 1), (None, 0), ("", 0)])
def test_existing_integer_and_no_reply_inputs_remain_supported(reply_id, expected):
    request = {"peer": "1", "text": "synthetic", "mode": "dry_run", "reply_to_message_id": reply_id}
    assert validate_send_request(request)[-1] == expected


def test_work_reply_without_job_binding_is_still_rejected():
    request = {"peer": "1", "text": "synthetic", "mode": "dry_run", "reply_to_message_id": "1"}
    reply_id = validate_send_request(request)[-1]
    monitor = object.__new__(DurableInboundMonitor)
    with pytest.raises(BridgeError, match="job_binding_required"):
        asyncio.run(
            _prepare_job_send(None, None, monitor, request, {"id": 1}, "synthetic", reply_id, "dry_run", "", False)
        )
