"""Local event-only work-Telegram to Codex adapter; no polling or replay."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import pwd
import signal
import socket
import stat
import struct
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from autostop_manager.telegram_bridge import INBOUND_EVENT_ID_PATTERN, MAX_INBOUND_MONITOR_EVENTS

LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/etc/autostop-work-telegram/wake.json")
SOCKET_PATH = Path("/run/autostop-codex-wake/wake.sock")
PROJECT_DIR = "/opt/AutostopManager"
APP_SOCKET = "/root/.codex/app-server-control/app-server-control.sock"
TASK_NAME = "Рабочий Telegram AutoStop"
RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)
CONNECT_TIMEOUT = 20
RECOVERY_POLL_INTERVAL = 15
HEARTBEAT_INTERVAL = 20
HEARTBEAT_TIMEOUT = 20
WAKE_INSTRUCTION = (
    "$manage-owner-telegram Новое событие рабочего Telegram: {event_id}; режим включён владельцем. "
    "Прочитай AGENTS.md: актуальные правила этого запуска заменяют устаревшие правила истории. "
    "Правила рабочего процесса находятся в docs/agent/modules/B2.md, контракт bridge — в "
    "docs/agent/modules/B4.md; .agents/skills/manage-owner-telegram/SKILL.md ведёт к этим источникам. "
    "Через work bridge разреши адресата по monitor-target для этого event_id, прочитай ближайший контекст "
    "и последние исходящие. При устаревшей ссылке сообщи об ошибке здесь и заверши ход без замены адресата. "
    "Доведи запрос до полезного результата по skill и заверши ход."
)
WAKE_INSTRUCTION_SHA256 = hashlib.sha256(WAKE_INSTRUCTION.encode("utf-8")).hexdigest()


class WakeError(RuntimeError):
    """Only fixed technical codes cross the logging boundary."""


class TransportLost(WakeError):
    """A connection failure; safe to retry only before attempting turn/start."""


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
        self.generation = 0
        self.managed = False
        self.blocked = False
        self.ready_event = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.recovery_state = "starting"
        self.recovery_error: str | None = None
        self.reconnect_attempts = 0
        self.last_disconnect_at: str | None = None
        self.last_disconnect_code: int | None = None

    async def connect(self) -> None:
        from websockets.asyncio.client import unix_connect
        from websockets.exceptions import WebSocketException

        await self._drop_connection()
        self.generation += 1
        generation = self.generation
        try:
            websocket = await unix_connect(
                self.config.app_socket,
                compression=None,
                ping_interval=HEARTBEAT_INTERVAL,
                ping_timeout=HEARTBEAT_TIMEOUT,
                open_timeout=10,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            )
        except WebSocketException as exc:
            raise TransportLost("codex_handshake_failed") from exc
        self.websocket = websocket
        self.connected = True
        self.disconnected.clear()
        self.reader = asyncio.create_task(self._read(websocket, generation))
        await self.request("initialize", {"clientInfo": {"name": "autostop_telegram_wake", "version": "1"}})
        await self._write({"method": "initialized", "params": {}})

    async def _drop_connection(self) -> None:
        self.generation += 1
        self.connected = False
        self.ready_event.clear()
        reader, websocket = self.reader, self.websocket
        self.reader = None
        self.websocket = None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        for future in self.pending.values():
            if not future.done():
                future.set_exception(TransportLost("codex_disconnected"))
        if websocket is not None:
            with suppress(OSError):
                await websocket.close()
        while not self.events.empty():
            self.events.get_nowait()
        # Wake a worker already awaiting notifications even if the supervisor
        # replaces the socket before that worker can consume the reader's signal.
        if self.active_turn:
            self.events.put_nowait({"method": "connection_lost"})

    async def _write(self, data: dict[str, Any]) -> None:
        from websockets.exceptions import WebSocketException

        if not self.connected or self.websocket is None:
            raise TransportLost("codex_disconnected")
        try:
            await self.websocket.send(json.dumps(data))
        except (OSError, WebSocketException) as exc:
            self.connected = False
            self.ready_event.clear()
            self.disconnected.set()
            raise TransportLost("codex_disconnected") from exc

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
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a concurrent disconnect after send failure.

    async def _read(self, websocket: Any, generation: int) -> None:
        from websockets.exceptions import WebSocketException

        try:
            async for line in websocket:
                if generation != self.generation:
                    return
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid_rpc_shape")
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            code = error.get("code") if isinstance(error, dict) else None
                            self.last_rpc_error_code = code if type(code) is int else None
                            future.set_exception(RPCRejected("codex_rpc_rejected"))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    error = {"code": -32601, "message": "unattended_request_not_supported"}
                    await self._write({"id": message["id"], "error": error})
                elif message.get("method") in {"turn/started", "turn/completed"}:
                    params = message.get("params", {})
                    if isinstance(params, dict) and params.get("threadId") == self.config.thread_id:
                        self.events.put_nowait(message)
        except (OSError, WebSocketException, TransportLost):
            pass
        except Exception:  # noqa: BLE001 - malformed/private RPC payloads fail closed with a fixed code.
            if generation == self.generation:
                self.block("codex_rpc_frame_invalid")
        finally:
            if generation == self.generation:
                close_code = getattr(websocket, "close_code", None)
                self.last_disconnect_at = datetime.now(UTC).isoformat()
                self.last_disconnect_code = close_code if type(close_code) is int else None
                LOGGER.warning(
                    "codex_connection_lost time=%s code=%s", self.last_disconnect_at, self.last_disconnect_code
                )
                self.connected = False
                if not self.blocked:
                    self.ready_event.clear()
                self.disconnected.set()
                if not self.pausing and not self.blocked:
                    self.recovery_state = "reconnecting"
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(TransportLost("codex_disconnected"))
                self.events.put_nowait({"method": "connection_lost"})

    async def resume(self, *, allow_active: bool = False) -> None:
        params = {"threadId": self.config.thread_id, "cwd": self.config.project_dir, "excludeTurns": True}
        try:
            result = await self.request("thread/resume", params)
        except RPCRejected as exc:
            raise WakeError("codex_thread_resume_rejected") from exc
        thread = rpc_entity(result, "thread")
        status = thread.get("status")
        if (
            thread["id"] != self.config.thread_id
            or thread.get("ephemeral")
            or thread.get("cwd") != self.config.project_dir
            or not isinstance(status, dict)
            or status.get("type") not in {"active", "idle", "notLoaded", "systemError"}
        ):
            raise WakeError("codex_thread_target_invalid")
        if not allow_active and status["type"] != "idle":
            raise WakeError("codex_thread_busy")

    def block(self, error: str) -> None:
        self.blocked = True
        self.recovery_state = "blocked"
        self.recovery_error = error
        LOGGER.warning("codex_recovery_blocked code=%s", error)
        self.ready_event.set()  # Release waiters so they can observe the block.
        self.disconnected.set()

    async def supervise(self) -> None:
        self.managed = True
        attempt = 0
        while not self.pausing and not self.blocked:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self.connect()
                    await self.resume(allow_active=True)
                if self.pausing or self.blocked:
                    return
                self.recovery_error = None
                self.recovery_state = "reconciling" if self.active_turn else "ready"
                self.ready_event.set()
                attempt = 0
                await self.disconnected.wait()
            except (OSError, TimeoutError, TransportLost):
                self.recovery_error = "codex_transport_failed_or_unknown"
            except WakeError as exc:
                self.block(str(exc))
                return
            except Exception:  # noqa: BLE001 - keep supervisor failures content-free and observable.
                self.block("codex_recovery_failed")
                return
            if self.pausing or self.blocked:
                return
            self.recovery_state = "reconnecting"
            self.ready_event.clear()
            await self._drop_connection()
            self.reconnect_attempts += 1
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def ensure_ready(self) -> None:
        while True:
            if self.managed and not (self.pausing or self.blocked):
                await self.ready_event.wait()
            if self.pausing:
                raise WakeError("codex_paused")
            if self.blocked:
                raise WakeError(self.recovery_error or "codex_recovery_blocked")
            if self.connected:
                return
            if not self.managed:
                raise TransportLost("codex_disconnected")
            # A reader may clear readiness just after this waiter's release.
            # The same event/known turn must wait for the next connection.
            self.ready_event.clear()
            self.disconnected.set()

    async def _turn_metadata(self) -> str:
        cursor = None
        for _ in range(5):
            params: dict[str, Any] = {
                "threadId": self.config.thread_id,
                "itemsView": "notLoaded",
                "sortDirection": "desc",
                "limit": 20,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.request("thread/turns/list", params)
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                raise WakeError("codex_turn_metadata_invalid")
            for turn in data:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise WakeError("codex_turn_metadata_invalid")
                if turn["id"] == self.active_turn:
                    status = turn.get("status")
                    if status not in {"completed", "inProgress", "failed", "interrupted"}:
                        raise WakeError("codex_turn_metadata_invalid")
                    return str(status)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor:
                raise WakeError("codex_turn_metadata_invalid")
        raise WakeError("codex_turn_not_found")

    def _complete_turn(self, status: Any, *, recovered: bool) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise WakeError("codex_turn_metadata_invalid")
        self.active_turn = None
        self.outcome_unknown = False
        if status != "completed":
            raise WakeError("codex_turn_failed")
        if not recovered and not self.last_turn_started:
            raise WakeError("codex_turn_start_not_observed")
        if not self.pausing:
            self.recovery_state = "ready"

    async def run_turn(self, text: str) -> None:
        self.last_turn_started = False
        turn_generation = await self._start_turn(text)
        await self._wait_turn(turn_generation)

    async def _start_turn(self, text: str) -> int:
        while True:
            await self.ensure_ready()
            try:
                async with self.start_lock:
                    if self.pausing:
                        raise WakeError("codex_paused")
                    self.last_rpc_error_code = None
                    resume_generation = self.generation
                    await self.resume()
                    if self.pausing:
                        raise WakeError("codex_paused")
                    # An idle response only authorizes the connection that
                    # produced it. A replacement must finish setup and pass
                    # its own idle check before it may receive turn/start.
                    if resume_generation != self.generation or (self.managed and not self.ready_event.is_set()):
                        continue
                    if not self.connected:
                        raise TransportLost("codex_disconnected")
                    turn_generation = self.generation
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
                return turn_generation
            except (TransportLost, OSError, TimeoutError):
                if not self.managed or self.outcome_unknown or self.pausing:
                    raise
                self.ready_event.clear()
                self.disconnected.set()

    async def _wait_turn(self, turn_generation: int) -> None:
        recovered = False
        while True:
            if self.managed and self.generation != turn_generation:
                recovered = True
            if not self.connected:
                if not self.managed or self.pausing:
                    raise WakeError("codex_turn_outcome_unknown")
                recovered = True
                await self.ensure_ready()
            if recovered and not self.pausing:
                if self.managed:
                    # A socket is marked connected before initialize/resume.
                    # Every metadata retry must wait for their completion.
                    await self.ensure_ready()
                self.recovery_state = "reconciling"
                turn_generation = self.generation
                try:
                    status = await self._turn_metadata()
                except (TransportLost, OSError, TimeoutError):
                    self.ready_event.clear()
                    self.disconnected.set()
                    continue
                if status != "inProgress":
                    self._complete_turn(status, recovered=True)
                    return
            try:
                if recovered and not self.pausing:
                    message = await asyncio.wait_for(self.events.get(), RECOVERY_POLL_INTERVAL)
                else:
                    message = await self.events.get()
            except TimeoutError:
                continue
            if message["method"] == "connection_lost":
                if not self.managed or self.pausing:
                    raise WakeError("codex_turn_outcome_unknown")
                recovered = True
                await self.ensure_ready()
                continue
            turn = message.get("params", {}).get("turn", {})
            if not isinstance(turn, dict) or turn.get("id") != self.active_turn:
                continue
            if message["method"] == "turn/started":
                self.last_turn_started = True
            elif message["method"] == "turn/completed":
                self._complete_turn(turn.get("status"), recovered=recovered)
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
        self.pausing = True
        self.recovery_state = "paused"
        await self._drop_connection()


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
        self.supervisor: asyncio.Task[None] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": self.enabled and not self.app.blocked,
            "ready": self.enabled and self.app.connected and self.app.recovery_state == "ready",
            "recovery_state": self.app.recovery_state,
            "reconnect_attempts": self.app.reconnect_attempts,
            "last_disconnect_at": self.app.last_disconnect_at,
            "last_disconnect_code": self.app.last_disconnect_code,
            "outcome_unknown": self.app.outcome_unknown,
            "connected": self.app.connected,
            "active": self.active,
            "queued": self.queue.qsize(),
            "accepted": self.accepted,
            "completed": self.completed,
            "failed": self.failed,
            "last_error": self.last_error or self.app.recovery_error,
            "rpc_error_code": self.app.last_rpc_error_code,
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
        if not self.enabled or self.app.blocked:
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

    def start(self) -> None:
        self.app.managed = True
        self.supervisor = asyncio.create_task(self.app.supervise())
        self.worker = asyncio.create_task(self.work())

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
                if not self.app.pausing:
                    self.app.block(self.last_error or "wake_worker_failed")
            finally:
                self.active = False
                self.queue.task_done()

    async def pause(self) -> None:
        self.enabled = False
        self.app.pausing = True
        self.app.recovery_state = "paused"
        self.app.ready_event.set()
        if self.supervisor is not None:
            self.supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await self.supervisor
        try:
            await self.app.interrupt()
            if self.worker is not None and not self.worker.done() and self.active:
                await asyncio.wait_for(asyncio.shield(self.worker), 20)
        finally:
            # Unknown pause outcomes stay visible, but no recovery timer or
            # pending worker may start later after control has been disabled.
            if self.worker is not None and not self.worker.done():
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
        SOCKET_PATH.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(dispatcher.serve, path=str(SOCKET_PATH), limit=1024)
        os.chmod(SOCKET_PATH, 0o660)
        os.chown(SOCKET_PATH, 0, pwd.getpwnam("autostop-work-telegram").pw_gid)
        dispatcher.start()
        if address := os.environ.get("NOTIFY_SOCKET"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
                ready.connect("\0" + address[1:] if address.startswith("@") else address)
                ready.sendall(b"READY=1")
        async with server:
            await stopping.wait()
            await dispatcher.pause()
    finally:
        if dispatcher.supervisor is not None:
            dispatcher.supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher.supervisor
        if dispatcher.worker is not None:
            dispatcher.worker.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher.worker
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
