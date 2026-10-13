"""Client routing and delivery contracts through native IPC, with synthetic peers only."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake
from test_telegram_durable_integration import (
    CLIENT,
    OTHER,
    binding,
    bridge_runtime,
    claim_running,
    send,
)
from test_telegram_durable_integration import bundle as bundle


def test_exact_outgoing_guard_accepts_real_telethon_private_message():
    types = pytest.importorskip("telethon.tl.types", reason="installed transport profile includes Telethon")
    message = types.Message(id=101, peer_id=types.PeerUser(CLIENT), message="Synthetic answer", out=True)
    assert bridge._outgoing_message_matches(message, peer_id=CLIENT, message_id=101)
    assert not bridge._outgoing_message_matches(message, peer_id=OTHER, message_id=101)
    assert not bridge._outgoing_message_matches(message, peer_id=CLIENT, message_id=102)


@pytest.mark.parametrize("delivery", ["client_result", "owner_handoff"])
@pytest.mark.parametrize("mismatch", ["message_id", "peer", "incoming", "group", "missing_identity"])
def test_forged_text_readback_cannot_verify_effect_or_finish_client_job(tmp_path, bundle, delivery, mismatch):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            event = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            text = "Synthetic answer" if delivery == "client_result" else "Synthetic request for a human"
            if delivery == "client_result":
                dry = await send(runtime, job, kind="result", text=text)
            else:
                request = binding(job) | {"reply_to_message_id": 100, "text": text, "mode": "dry_run"}
                dry = await runtime.jobs.request("job_handoff", **request)
            original = runtime.client.get_messages

            async def forged(entity, *, ids=None, limit=None):
                result = await original(entity, ids=ids, limit=limit)
                if ids is None or result is None or not result.out:
                    return result
                values = vars(result).copy()
                if mismatch == "message_id":
                    values["id"] += 1
                elif mismatch == "peer":
                    values["chat_id"] = OTHER
                elif mismatch == "incoming":
                    values["out"] = False
                elif mismatch == "group":
                    values["is_private"] = False
                else:
                    for field in ("id", "chat_id", "out", "is_private"):
                        values.pop(field)
                return SimpleNamespace(**values)

            runtime.client.get_messages = forged
            with pytest.raises(wake.WakeError, match=r"^send_readback_failed$"):
                if delivery == "client_result":
                    await send(runtime, job, kind="result", text=text, dry=dry)
                else:
                    await runtime.jobs.request(
                        "job_handoff",
                        **(
                            request
                            | {
                                "mode": "apply",
                                "contract_token": dry["contract_token"],
                                "idempotency_key": dry["idempotency_key"],
                            }
                        ),
                    )
            effect = runtime.inbox.read_effect(dry["idempotency_key"])
            assert effect["attempted"] == 1 and effect["verified"] == 0
            assert runtime.inbox.event_state(event) == "open" and runtime.client.sent == 1
            outcome = "answered" if delivery == "client_result" else "handoff"
            with pytest.raises(wake.WakeError, match=r"^inbox_effect_outcome_unknown$"):
                await runtime.jobs.request(
                    "job_result", **binding(job), outcome=outcome, event_ids=[event], receipt_keys=[]
                )
            runtime.client.get_messages = original
            proof = await runtime.jobs.request("job_effect_readback", **binding(job), effect_key=dry["idempotency_key"])
            assert proof["verified"] and runtime.client.sent == 1
            result = await runtime.jobs.request(
                "job_result", **binding(job), outcome=outcome, event_ids=[event], receipt_keys=[proof["receipt_key"]]
            )
            assert result["job"]["state"] == "completed" and runtime.inbox.event_state(event) == "reply_verified"
            assert runtime.client.sent == 1
            if delivery == "client_result":
                assert runtime.client.messages[proof["message_id"]].chat_id == CLIENT

    asyncio.run(scenario())
