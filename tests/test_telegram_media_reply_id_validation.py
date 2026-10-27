"""Media reply types are checked before file access or peer resolution."""

import asyncio
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake
from test_telegram_durable_integration import binding, bridge_runtime, claim_running
from test_telegram_durable_integration import bundle as bundle


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
@pytest.mark.parametrize("reply_id", [0.0, 0.9, 1.0, 1.9, True, False, [], {}])
@pytest.mark.parametrize("durable", [True, False])
def test_media_rejects_invalid_reply_before_io(monkeypatch, operation, reply_id, durable):
    def denied(*_args, **_kwargs):
        raise AssertionError("invalid reply must not access a media file")

    monkeypatch.setattr(bridge, "_load_validated_photo_file", denied)
    monkeypatch.setattr(bridge, "_load_validated_document_file", denied)
    request = {
        "operation": operation,
        "peer": "42",
        "photo": "/unused.jpg",
        "document": "/unused.pdf",
        "caption": "synthetic",
        "reply_to_message_id": reply_id,
    }
    monitor = object.__new__(bridge.DurableInboundMonitor) if durable else None
    config = SimpleNamespace(account="work")
    with pytest.raises(bridge.BridgeError, match="reply_to_message_id_invalid"):
        asyncio.run(bridge._handle_operation(None, config, request, inbound_monitor=monitor))


@pytest.mark.parametrize("reply_id", [0, 1, "", "1", None])
def test_existing_media_reply_input_types_remain_accepted_by_validation(reply_id):
    request = {"peer": "42", "caption": "synthetic", "reply_to_message_id": reply_id}
    bridge.validate_photo_request(request)
    bridge.validate_document_request(request)


@pytest.mark.parametrize("reply_id", [0.0, 0.9, 100.0, 100.9])
def test_handoff_rejects_float_reply_without_effect(tmp_path, bundle, reply_id):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            with pytest.raises(wake.WakeError, match="inbound_message_invalid"):
                await runtime.jobs.request(
                    "job_handoff", **(binding(job) | {"reply_to_message_id": reply_id, "text": "Synthetic"})
                )
            assert runtime.client.sent == 0
            assert runtime.inbox.read_job(job["job_id"])["effects"] == []

    asyncio.run(scenario())
