"""Restart the work bridge after Telegram accepts a send but before it returns an ID."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import signal
import socket
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from test_telegram_durable_integration import (
    CLIENT,
    OTHER,
    REVISION,
    TelegramClient,
    binding,
    claim_running,
    read_history,
)

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import TelegramInbox
from autostop_manager.telegram_wake import WakeError

TIMEOUT = 10
REQUESTS = {CLIENT: 100, OTHER: 200}
ANSWER = "Synthetic reply"


def _accepted(root):
    with (root / "accepted.jsonl").open() as records:
        return [json.loads(line) for line in records]


class RestartClient(TelegramClient):
    def __init__(self, root, interrupt):
        super().__init__()
        self.root = root
        self.interrupt = interrupt
        self.disconnected = asyncio.get_running_loop().create_future()
        self.session = SimpleNamespace(save_entities=True)
        self._sender = SimpleNamespace(_auto_reconnect_callback=self.connect, _start_reconnect=lambda _error: None)
        received_at = datetime.fromisoformat((root / "received-at").read_text())
        for peer, identifier in REQUESTS.items():
            message = self.inbound(identifier, peer=peer)
            message.date = received_at
            message.sender_id = peer
        for row in _accepted(root):
            self._remember_sent(row)

    def _remember_sent(self, row):
        message = self.inbound(row["id"], peer=row["peer"], text=row["text"])
        message.date = datetime.fromisoformat(row["date"])
        message.out = True
        message.reply_to_msg_id = row["reply_to"]
        return message

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=900001)

    def add_event_handler(self, *_args):
        pass

    async def __call__(self, _state_request):
        return SimpleNamespace(pts=len(self.messages), qts=0, seq=0)

    async def iter_dialogs(self, *, limit):
        dialogs = []
        for peer in REQUESTS:
            entity = await self.get_entity(peer)
            latest = (await self.get_messages(entity, limit=1))[0]
            dialogs.append(SimpleNamespace(entity=entity, id=peer, is_user=True, message=latest))
        for dialog in sorted(dialogs, key=lambda item: item.message.date, reverse=True)[:limit]:
            yield dialog

    async def iter_messages(self, entity, *, min_id, reverse, limit):
        messages = await self.get_messages(entity)
        matching = sorted((message.id for message in messages if message.id > min_id), reverse=not reverse)
        for identifier in matching[:limit]:
            yield self.messages[identifier]

    async def send_message(self, entity, text, *, reply_to, parse_mode):
        row = {
            "id": max(self.messages) + 1,
            "peer": entity.id,
            "text": text,
            "reply_to": reply_to,
            "date": datetime.now(UTC).isoformat(),
        }
        message = self._remember_sent(row)
        # The simulated remote acceptance must survive the loss of this process.
        with (self.root / "accepted.jsonl").open("a") as records:
            records.write(json.dumps(row) + "\n")
        if self.interrupt:
            print("message accepted", flush=True)
            await asyncio.Event().wait()
        return message


def _run_daemon(root, interrupt):
    def require_local_daemon(event, args):
        if event in {"socket.connect", "subprocess.Popen", "os.system", "os.posix_spawn"} or (
            event == "socket.__new__" and args[1] != socket.AF_UNIX
        ):
            raise AssertionError(f"unexpected external action in test daemon: {event}")

    def require_test_client(writer):
        connection = writer.get_extra_info("socket")
        assert connection.family == socket.AF_UNIX
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if (pid, uid) != (os.getppid(), os.geteuid()):
            raise bridge.BridgeError("job_control_permission_denied")

    sys.addaudithook(require_local_daemon)
    os.environ[bridge.WORK_DURABLE_ENVIRONMENT] = "1"
    bridge.WORK_OWNER_CONFIG_PATH = root / "owner.json"
    config = bridge.TelegramConfig(
        api_id=123456,
        api_hash="0" * 32,
        session_path=root / "state" / "account",
        state_dir=root / "state",
        socket_path=root / "bridge.sock",
        owner_config_path=bridge.WORK_OWNER_CONFIG_PATH,
    )
    bridge._load_telethon = lambda: (
        lambda *_args, **_kwargs: RestartClient(root, interrupt),
        SimpleNamespace(get_peer_id=lambda entity: entity.id),
        None,
    )
    bridge._load_telegram_events = lambda: SimpleNamespace(
        NewMessage=lambda **_kwargs: object(), MessageEdited=object, MessageDeleted=object
    )
    bridge._load_reconciliation_state_request = object
    bridge._configure_durable_instructions = lambda inbox: inbox.configure_instructions(str(root), REVISION)
    bridge._owner_peer_id = lambda *_args: None
    bridge._verified_reconnect_start = lambda sender, start: start is sender._start_reconnect
    # Only the parent of this private daemon can make privileged test RPCs.
    bridge._require_root_client = require_test_client
    asyncio.run(bridge.run_daemon(config, monitor_incoming=True))


class LocalJobs:
    """Exercise the daemon's Unix RPC as the test runner on a private socket."""

    def __init__(self, path):
        self.path = path

    async def request(self, operation, **params):
        async with asyncio.timeout(TIMEOUT):
            reader, writer = await asyncio.open_unix_connection(self.path)
            try:
                writer.write(json.dumps({"operation": operation, **params}).encode() + b"\n")
                await writer.drain()
                response = await reader.readline()
            finally:
                writer.close()
                await writer.wait_closed()
        if not response:
            raise EOFError("bridge exited before replying")
        result = json.loads(response)
        if not result["ok"]:
            raise WakeError(result["error"])
        return result


@asynccontextmanager
async def _daemon(root, jobs, *, interrupt):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(root),
        "interrupt" if interrupt else "resume",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AUTOSTOP_MANAGER_ENV_FILE": "/dev/null",
        },
    )
    try:
        async with asyncio.timeout(TIMEOUT):
            while process.returncode is None:
                try:
                    status = await jobs.request("monitor_status")
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(0.01)
                    continue
                monitor = status["monitor"]
                if monitor["intake_reconciliation"] == "complete":
                    break
                assert monitor["intake_reconciliation"] != "held", monitor["intake_health"]
                await asyncio.sleep(0.01)
            else:
                pytest.fail(f"bridge exited during startup: {(await process.stdout.read()).decode()}")
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), TIMEOUT)


async def _send(jobs, job, *, slot=0, prepared=None):
    request = {
        "peer": str(job["peer_id"]),
        "text": ANSWER,
        "reply_to_message_id": REQUESTS[job["peer_id"]],
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "result",
        "effect_slot": slot,
        "mode": "dry_run",
    }
    if prepared is not None:
        request.update(
            mode="apply", contract_token=prepared["contract_token"], idempotency_key=prepared["idempotency_key"]
        )
    return await jobs.request("send", **request)


async def _close_job(jobs, job, receipt_keys):
    response = await jobs.request(
        "job_result", **binding(job), outcome="answered", event_ids=job["event_ids"], receipt_keys=receipt_keys
    )
    return response["job"]


async def _check_restart(root):
    (root / "accepted.jsonl").touch()
    received_at = datetime.now(UTC).replace(microsecond=0)
    (root / "received-at").write_text(received_at.isoformat())
    inbox = TelegramInbox(root / "state" / "inbound.sqlite3")
    try:
        inbox.duty_set(True, at=(received_at - timedelta(hours=1)).isoformat())
    finally:
        inbox.close()

    jobs = LocalJobs(root / "bridge.sock")
    runtime = SimpleNamespace(jobs=jobs)
    async with _daemon(root, jobs, interrupt=True) as first:
        job = await claim_running(runtime, root)
        assert job["peer_id"] == CLIENT
        prepared = await _send(jobs, job)
        key = prepared["idempotency_key"]
        task = asyncio.create_task(_send(jobs, job, prepared=prepared))
        try:
            marker = await asyncio.wait_for(first.stdout.readline(), TIMEOUT)
            assert marker == b"message accepted\n"
            first.kill()
            with pytest.raises(EOFError, match="bridge exited before replying"):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert first.returncode == -signal.SIGKILL
    (accepted,) = _accepted(root)
    assert accepted["peer"] == CLIENT
    ledger_path = root / "state" / "idempotency.json"
    assert json.loads(ledger_path.read_text())[key]["message_id"] == 0

    async with _daemon(root, jobs, interrupt=False):
        recovered = (await jobs.request("job_read", job_id=job["job_id"]))["job"]
        (effect,) = recovered["effects"]
        assert effect["effect_key"] == key and effect["attempted"] == 1 and effect["verified"] == 0
        await read_history(runtime, recovered)
        with pytest.raises(WakeError, match=r"^send_outcome_uncertain$"):
            await _send(jobs, recovered, prepared=prepared)
        with pytest.raises(WakeError, match=r"^send_outcome_uncertain$"):
            await _send(jobs, recovered, slot=1)
        with pytest.raises(WakeError, match=r"^send_outcome_uncertain$"):
            await _close_job(jobs, recovered, [])

        independent = await claim_running(runtime, root)
        assert independent["peer_id"] == OTHER
        other_prepared = await _send(jobs, independent)
        receipt = await _send(jobs, independent, prepared=other_prepared)
        finished = await _close_job(jobs, independent, [receipt["receipt_key"]])
        assert finished["state"] == "completed"
        assert finished["effects"][0]["verified"]
        assert (await jobs.request("job_read", job_id=job["job_id"]))["job"]["state"] == "running"

        proof_request = {**binding(recovered), "effect_key": key, "effects_reviewed": True}
        with pytest.raises(WakeError, match=r"^job_effect_readback_failed$"):
            await jobs.request("job_effect_readback", **proof_request, message_id=receipt["message_id"])
        assert json.loads(ledger_path.read_text())[key]["message_id"] == 0
        assert not (await jobs.request("job_read", job_id=job["job_id"]))["job"]["effects"][0]["verified"]
        proof = await jobs.request("job_effect_readback", **proof_request, message_id=accepted["id"])
        assert proof["verified"] and proof["receipt_key"] == key and proof["message_id"] == accepted["id"]
        delivered = json.loads(ledger_path.read_text())[key]
        assert delivered["readback_verified"] and delivered["message_id"] == accepted["id"]
        verified = (await jobs.request("job_read", job_id=job["job_id"]))["job"]["effects"][0]
        assert verified["verified"] and verified["receipt_key"] == key
        assert [(row["peer"], row["reply_to"]) for row in _accepted(root)] == list(REQUESTS.items())


def test_bridge_restart_fences_uncertain_send_without_blocking_another_client():
    # Keep the Unix socket path short even when pytest uses a long basetemp.
    with TemporaryDirectory(prefix="telegram-restart-", dir="/tmp") as directory:
        asyncio.run(_check_restart(Path(directory)))


if __name__ == "__main__":
    _run_daemon(Path(sys.argv[1]), interrupt=sys.argv[2] == "interrupt")
