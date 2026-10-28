"""Native job delivery with optional media, using only synthetic transport."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_telegram_bridge_durable import (
    MediaClient,
    _job,
    _message,
    _runtime,
    _send,
)
from test_telegram_bridge_durable import synthetic_state_request as synthetic_state_request
from test_telegram_media_native import _apply, _invoke
from test_telegram_media_native import media_case as media_case

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


@pytest.mark.parametrize("kind,suffix", [("photo", ".jpg"), ("document", ".pdf")])
def test_unavailable_optional_media_before_intent_allows_verified_text_result(tmp_path, kind, suffix):
    async def scenario():
        config, monitor = _runtime(tmp_path)
        try:
            source = _message(100)
            monitor.record(source)
            job = _job(monitor)
            client = MediaClient([source])
            outbox = config.socket_path.parent / "outbox"
            outbox.mkdir(mode=0o700)
            request = {
                "operation": "send_" + kind,
                "peer": "42",
                kind: str(outbox / ("unavailable" + suffix)),
                "caption": "Synthetic technical illustration",
                "job_id": job["job_id"],
                "lease": job["lease"],
                "revision": job["revision"],
                "kind": "result",
                "reply_to_message_id": 100,
            }
            with pytest.raises(bridge.BridgeError):
                await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            assert client.sent == 0 and monitor.inbox.read_job(job["job_id"])["effects"] == []
            dry = await _send(client, config, monitor, job, kind="result", text="Synthetic useful text")
            receipt = await _send(
                client, config, monitor, job, kind="result", text="Synthetic useful text", mode="apply", dry=dry
            )
            completed = monitor.inbox.result(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                outcome="answered",
                event_ids=job["event_ids"],
                receipt_keys=[receipt["receipt_key"]],
            )
            assert completed["state"] == "completed" and receipt["verified"] is True
            assert client.sent == 1 and monitor.inbox.status()["open_events"] == 0
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_unknown_media_delivery_blocks_fallback_text_and_false_completion(media_case):
    inbox = TelegramInbox(media_case.config.state_dir / "inbound.sqlite3")
    try:
        inbox.duty_set(True, at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
        monitor.record(media_case.client.source)
        job = _job(monitor)
        request = media_case.request("photo") | {
            "job_id": job["job_id"],
            "lease": job["lease"],
            "revision": job["revision"],
            "kind": "result",
            "reply_to_message_id": 100,
        }
        dry = _invoke(media_case, request, monitor=monitor)
        media_case.client.fault = "id"
        with pytest.raises(bridge.BridgeError, match="send_readback_failed"):
            _apply(media_case, request, dry, monitor=monitor)
        with pytest.raises((bridge.BridgeError, InboxError), match="inbox_effect_outcome_unknown"):
            asyncio.run(
                _send(
                    media_case.client,
                    media_case.config,
                    monitor,
                    job,
                    kind="result",
                    text="Synthetic fallback",
                    slot=1,
                )
            )
        with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
            inbox.result(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[],
                reason_code="not_actionable",
            )
        assert media_case.client.sent == 1 and inbox.read_job(job["job_id"])["state"] == "running"
        assert inbox.read_job(job["job_id"])["effects"][0]["verified"] == 0
    finally:
        inbox.close()
