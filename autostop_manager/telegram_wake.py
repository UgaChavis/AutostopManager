"""Local event-only work-Telegram to Codex adapter; no polling or replay."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pwd
import signal
import socket
import stat
import struct
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from autostop_manager.telegram_bridge import INBOUND_EVENT_ID_PATTERN, MAX_INBOUND_MONITOR_EVENTS

CONFIG_PATH = Path("/etc/autostop-work-telegram/wake.json")
SOCKET_PATH = Path("/run/autostop-codex-wake/wake.sock")
PROJECT_DIR = "/opt/AutostopManager"
APP_SOCKET = "/root/.codex/app-server-control/app-server-control.sock"
TASK_NAME = "Рабочий Telegram AutoStop"
WAKE_INSTRUCTION = (
    "$manage-owner-telegram Новое событие рабочего Telegram: {event_id}; режим включён владельцем. "
    "Прочитай AGENTS.md: актуальные правила этого запуска заменяют устаревшие правила истории. "
    "Используй актуальный Telegram skill, приложенный в этом ходе; если его нет, прочитай "
    ".agents/skills/manage-owner-telegram/SKILL.md. "
    "Через work bridge разреши адресата по monitor-target для этого event_id, прочитай ближайший контекст "
    "и последние исходящие. При устаревшей ссылке сообщи об ошибке здесь и заверши ход без замены адресата. "
    "Доведи запрос до полезного результата по skill и заверши ход."
)
WAKE_INSTRUCTION_SHA256 = hashlib.sha256(WAKE_INSTRUCTION.encode("utf-8")).hexdigest()


class WakeError(RuntimeError):
    """Only fixed technical codes cross the logging boundary."""


class RPCRejected(WakeError):
    """The server explicitly rejected a request; distinct from a lost response."""


def rpc_entity(result: Any, key: str) -> dict[str, Any]:
    """Validate response entities before using them to route or track a turn."""
    entity = result.get(key) if isinstance(result, dict) else None
    if not isinstance(entity, dict) or not isinstance(entity.get("id"), str) or not entity["id"]:
        raise WakeError("codex_rpc_response_invalid")
    return entity


@dataclass(frozen=True)
class WakeConfig:
    thread_id: str
    project_dir: str = PROJECT_DIR
    app_socket: str = APP_SOCKET

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> WakeConfig:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise WakeError("wake_config_permissions")
        result = cls(**json.loads(path.read_text()))
        UUID(result.thread_id)
        if (result.project_dir, result.app_socket) != (PROJECT_DIR, APP_SOCKET):
            raise WakeError("wake_config_target_invalid")
        return result


class AppServer:
    """JSON-RPC over the local WebSocket; payloads stay in memory, never in logs."""

    def __init__(self, config: WakeConfig) -> None:
        self.config = config
        self.websocket: Any = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sequence = 0
        self.connected = False
        self.active_turn: str | None = None
        self.last_turn_started = False
        self.start_lock = asyncio.Lock()
        self.pausing = False
        self.outcome_unknown = False
        self.last_rpc_error_code: int | None = None

    async def connect(self) -> None:
        from websockets.asyncio.client import unix_connect
        from websockets.exceptions import WebSocketException

        try:
            self.websocket = await unix_connect(
                self.config.app_socket,
                compression=None,
                ping_interval=None,
                open_timeout=10,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            )
        except WebSocketException as exc:
            raise WakeError("codex_handshake_failed") from exc
        self.connected = True
        self.reader = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "autostop_telegram_wake", "version": "1"}})
        await self._write({"method": "initialized", "params": {}})

    async def _write(self, data: dict[str, Any]) -> None:
        from websockets.exceptions import WebSocketException

        if not self.connected or self.websocket is None:
            raise WakeError("codex_disconnected")
        try:
            await self.websocket.send(json.dumps(data))
        except WebSocketException as exc:
            raise WakeError("codex_disconnected") from exc

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self._write({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(ident, None)

    async def _read(self) -> None:
        from websockets.exceptions import WebSocketException

        try:
            async for line in self.websocket:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid_rpc_shape")
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            code = message["error"].get("code")
                            self.last_rpc_error_code = code if type(code) is int else None
                            future.set_exception(RPCRejected("codex_rpc_rejected"))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    # Never grant a privilege request on behalf of the owner.
                    error = {"code": -32601, "message": "unattended_request_not_supported"}
                    await self._write({"id": message["id"], "error": error})
                elif message.get("method") in {"turn/started", "turn/completed"}:
                    params = message.get("params", {})
                    if params.get("threadId") == self.config.thread_id:
                        self.events.put_nowait(message)
        except (OSError, ValueError, WebSocketException):
            pass
        finally:
            self.connected = False
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(WakeError("codex_disconnected"))
            self.events.put_nowait({"method": "connection_lost"})

    async def resume(self, *, allow_active: bool = False) -> None:
        params = {"threadId": self.config.thread_id, "cwd": self.config.project_dir}
        try:
            result = await self.request("thread/resume", params)
        except RPCRejected as exc:
            raise WakeError("codex_thread_resume_rejected") from exc
        thread = rpc_entity(result, "thread")
        if (
            thread["id"] != self.config.thread_id
            or thread.get("ephemeral")
            or thread.get("cwd") != self.config.project_dir
        ):
            raise WakeError("codex_thread_target_invalid")
        if not allow_active and thread.get("status", {}).get("type") == "active":
            raise WakeError("codex_thread_busy")

    async def run_turn(self, text: str) -> None:
        self.last_turn_started = False
        async with self.start_lock:
            if self.pausing:
                raise WakeError("codex_paused")
            # A persisted task can be unloaded between messages. Reopen it for
            # each event, without retrying any generation or changing its id.
            self.last_rpc_error_code = None
            await self.resume()
            if self.pausing:
                raise WakeError("codex_paused")
            self.outcome_unknown = True
            try:
                result = await self.request(
                    "turn/start",
                    {
                        "threadId": self.config.thread_id,
                        "cwd": self.config.project_dir,
                        "input": [{"type": "text", "text": text}],
                    },
                )
            except RPCRejected as exc:
                self.outcome_unknown = False
                raise WakeError("codex_turn_start_rejected") from exc
            self.active_turn = rpc_entity(result, "turn")["id"]
        while True:
            message = await self.events.get()
            if message["method"] == "connection_lost":
                raise WakeError("codex_turn_outcome_unknown")
            turn = message.get("params", {}).get("turn", {})
            if turn.get("id") != self.active_turn:
                continue
            if message["method"] == "turn/started":
                self.last_turn_started = True
            elif message["method"] == "turn/completed":
                self.active_turn = None
                self.outcome_unknown = False
                if turn.get("status") != "completed":
                    raise WakeError("codex_turn_failed")
                if not self.last_turn_started:
                    raise WakeError("codex_turn_start_not_observed")
                return

    async def interrupt(self) -> None:
        self.pausing = True
        async with asyncio.timeout(20), self.start_lock:
            if self.outcome_unknown and (not self.active_turn or not self.connected):
                raise WakeError("wake_pause_outcome_unknown")
            if self.active_turn:
                await self.request(
                    "turn/interrupt",
                    {"threadId": self.config.thread_id, "turnId": self.active_turn},
                )

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader


class WakeDispatcher:
    def __init__(self, app: AppServer, bridge_uid: int) -> None:
        self.app = app
        self.bridge_uid = bridge_uid
        self.queue: asyncio.Queue[str] = asyncio.Queue(MAX_INBOUND_MONITOR_EVENTS)
        self.seen: deque[str] = deque(maxlen=MAX_INBOUND_MONITOR_EVENTS * 2)
        self.enabled = True
        self.active = False
        self.accepted = 0
        self.completed = 0
        self.failed = 0
        self.last_error: str | None = None
        self.worker: asyncio.Task[None] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": self.enabled,
            "connected": self.app.connected,
            "active": self.active,
            "queued": self.queue.qsize(),
            "accepted": self.accepted,
            "completed": self.completed,
            "failed": self.failed,
            "last_error": self.last_error,
            "rpc_error_code": getattr(self.app, "last_rpc_error_code", None),
            "retention": "memory_only",
            "trigger": "telegram_event",
            "polling": False,
            "instruction_sha256": WAKE_INSTRUCTION_SHA256,
        }

    def accept(self, request: dict[str, Any], uid: int) -> dict[str, Any]:
        if uid != self.bridge_uid:
            raise WakeError("wake_sender_denied")
        if set(request) != {"operation", "event_id"} or request.get("operation") != "event":
            raise WakeError("wake_request_invalid")
        event_id = request["event_id"]
        if not isinstance(event_id, str) or not INBOUND_EVENT_ID_PATTERN.fullmatch(event_id):
            raise WakeError("wake_event_invalid")
        if not self.enabled or not self.app.connected:
            raise WakeError("wake_not_ready")
        if event_id in self.seen:
            return {"ok": True, "duplicate": True}
        if self.queue.full():
            self.last_error = "wake_queue_full"
            self.failed += 1
            raise WakeError("wake_queue_full")
        self.queue.put_nowait(event_id)
        self.seen.append(event_id)
        self.accepted += 1
        return {"ok": True}

    async def work(self) -> None:
        while self.enabled:
            event_id = await self.queue.get()
            self.active = True
            try:
                await self.app.run_turn(WAKE_INSTRUCTION.format(event_id=event_id))
                self.completed += 1
            except Exception as exc:  # noqa: BLE001 - fail closed without exposing private RPC payloads.
                if self.enabled or self.app.outcome_unknown:
                    self.failed += 1
                    if isinstance(exc, WakeError):
                        self.last_error = str(exc)
                    elif isinstance(exc, (OSError, TimeoutError)):
                        self.last_error = "codex_transport_failed_or_unknown"
                    else:
                        self.last_error = "wake_worker_failed"
                # Unknown side effects are not replayed and no new turn overlaps them.
                self.enabled = False
            finally:
                self.active = False
                self.queue.task_done()

    async def pause(self) -> None:
        self.enabled = False
        await self.app.interrupt()
        if self.worker is not None and not self.worker.done():
            if self.active:
                await asyncio.wait_for(asyncio.shield(self.worker), 20)
            else:
                self.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await self.worker
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            uid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            raw = await asyncio.wait_for(reader.readline(), 3)
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise WakeError("wake_request_invalid")
            if uid == 0 and request == {"operation": "status"}:
                response = self.status()
            elif uid == 0 and request == {"operation": "pause"}:
                await self.pause()
                response = self.status()
            else:
                response = self.accept(request, uid)
        except (ValueError, OSError, TimeoutError, WakeError) as exc:
            response = {"ok": False, "error": str(exc) if isinstance(exc, WakeError) else "wake_request_failed"}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def local_request(request: dict[str, Any], path: Path = SOCKET_PATH) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(path), limit=4096)
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        return dict(json.loads(await asyncio.wait_for(reader.readline(), 45)))
    finally:
        writer.close()
        await writer.wait_closed()


async def daemon(config: WakeConfig) -> None:
    app = AppServer(config)
    dispatcher = WakeDispatcher(app, pwd.getpwnam("autostop-work-telegram").pw_uid)
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    try:
        await app.connect()
        # Startup only attaches; an owner's active turn must not prevent readiness.
        # Each actual event still uses run_turn's strict idle-target check.
        await app.resume(allow_active=True)
        SOCKET_PATH.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(dispatcher.serve, path=str(SOCKET_PATH), limit=1024)
        os.chmod(SOCKET_PATH, 0o660)
        os.chown(SOCKET_PATH, 0, pwd.getpwnam("autostop-work-telegram").pw_gid)
        dispatcher.worker = asyncio.create_task(dispatcher.work())
        if address := os.environ.get("NOTIFY_SOCKET"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
                ready.connect("\0" + address[1:] if address.startswith("@") else address)
                ready.sendall(b"READY=1")
        async with server:
            await stopping.wait()
            await dispatcher.pause()
    finally:
        await app.close()
        SOCKET_PATH.unlink(missing_ok=True)


async def setup_task(*, probe: bool = False) -> dict[str, Any]:
    if not probe and CONFIG_PATH.exists():
        WakeConfig.load()
        return {"ok": True, "existing": True}
    app = AppServer(WakeConfig(thread_id=""))
    try:
        await app.connect()
        params: dict[str, Any] = {"cwd": PROJECT_DIR, "ephemeral": False}
        if probe:
            params["sandbox"] = "read-only"
            params["approvalPolicy"] = "never"
            params["developerInstructions"] = (
                "Технический тест транспорта. Не вызывай инструменты и не читай файлы. Ответь только WAKE_PROBE_OK."
            )
        result = await app.request("thread/start", params)
        ident = result["thread"]["id"]
        app.config = WakeConfig(thread_id=ident)
        await app.request("thread/name/set", {"threadId": ident, "name": "AutoStop wake probe" if probe else TASK_NAME})
        if probe:
            await asyncio.wait_for(app.run_turn("Ответь только WAKE_PROBE_OK."), 180)
            result = await app.request("thread/read", {"threadId": ident, "includeTurns": True})
            turns = result["thread"].get("turns", [])
            items = turns[-1].get("items", []) if turns else []
            if not any(i.get("type") == "agentMessage" and i.get("text", "").strip() == "WAKE_PROBE_OK" for i in items):
                raise WakeError("codex_probe_output_invalid")
            await app.request("thread/archive", {"threadId": ident})
            return {
                "ok": True,
                "turn_started": app.last_turn_started,
                "turn_completed": True,
                "output_verified": True,
                "instruction_sha256": WAKE_INSTRUCTION_SHA256,
            }
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(asdict(app.config), target)
            target.write("\n")
        return {"ok": True, "created": True}
    finally:
        await app.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["daemon", "status", "pause", "setup", "probe"])
    args = parser.parse_args()
    try:
        if args.command == "daemon":
            asyncio.run(daemon(WakeConfig.load()))
            return 0
        if args.command in {"setup", "probe"}:
            result = asyncio.run(setup_task(probe=args.command == "probe"))
        else:
            result = asyncio.run(local_request({"operation": args.command}))
    except (OSError, ValueError, TypeError, KeyError, WakeError, TimeoutError) as exc:
        result = {"ok": False, "error": str(exc) if isinstance(exc, WakeError) else "wake_operation_failed"}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
