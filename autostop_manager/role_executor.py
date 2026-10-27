"""Short M1/M2 dispatch/reconciliation in the existing automation daemon.

Codex owns long running goals; this controller never waits for a goal to finish.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .role_coordination import MODEL, PROJECT_DIR, ROLES, RoleError, RoleStore, executor_host_id, now


APP_SOCKET = "/root/.codex/app-server-control/app-server-control.sock"


class RoleRpcRejected(RoleError):
    pass


class CodexRoleRPC:
    """A bounded request connection; no rollout text or notifications enter logs."""

    def __init__(self, websocket: Any, timeout: float = 5) -> None:
        self.websocket = websocket
        self.timeout = timeout
        self.sequence = 0

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        ident = self.sequence
        async with asyncio.timeout(self.timeout):
            await self.websocket.send(json.dumps({"id": ident, "method": method, "params": params}))
            while True:
                message = json.loads(await self.websocket.recv())
                if message.get("id") != ident or "method" in message:
                    continue
                if "error" in message:
                    raise RoleRpcRejected("role_rpc_rejected")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RoleError("role_rpc_response_invalid")
                return result


@asynccontextmanager
async def role_rpc():
    from websockets.asyncio.client import unix_connect

    async with unix_connect(
        APP_SOCKET, compression=None, open_timeout=5, close_timeout=1, max_size=16 * 1024 * 1024
    ) as websocket:
        rpc = CodexRoleRPC(websocket)
        await rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "autostop_role_controller", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        yield rpc


def role_objective(role: str) -> str:
    return (
        f"Дежурство AutoStop {role}: выполняй актуальную инструкцию кабинета {role}, сохраняй состояние через "
        "manager_roles/manager_role_action, работай до команды владельца закончить дежурство. "
        "Самостоятельно выполняй только разрешённую работу, новые полномочия согласовывай. "
        "Ожидание оформляй manager_role_action sleep на 1–3600 секунд; не обходи ручную остановку и лимиты."
    )


def instruction_bundle(state: dict[str, Any], item: dict[str, Any] | None) -> tuple[str, str]:
    data = item["data"] if item else {}
    root = data.get("instruction_root") or state.get("instruction_root")
    revision = data.get("instruction_revision") or state.get("instruction_revision")
    path = Path(root) if root else Path("/opt/autostop-manager-releases/current").resolve()
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise RoleError("role_instruction_unavailable")
    installed = (path / "REVISION").read_text().strip()
    if revision and revision != installed:
        raise RoleError("role_instruction_revision_mismatch")
    for name in ("AGENTS.md", f"docs/agent/modules/{state['role']}.md"):
        if not (path / name).is_file():
            raise RoleError("role_instruction_unavailable")
    return str(path), installed


def dispatch_prompt(state: dict[str, Any], dispatch: dict[str, Any], item: dict[str, Any] | None) -> str:
    root, revision = instruction_bundle(state, item)
    role = state["role"]
    text = (
        f"AutoStop {role}; dispatch_id={dispatch['id']}. Физический корень инструкций {root}, REVISION {revision}. "
        f"Прочитай {root}/AGENTS.md, {root}/docs/agent/modules/A1.md и {root}/docs/agent/modules/{role}.md. "
        "Cwd остаётся /opt/AutostopManager. Через manager_roles перечитай состояние, задачи, вопросы и разрешения. "
        "Рабочие знания роли не повышают полномочия. Не пиши клиентам от роли директора. "
    )
    if item and item["kind"] == "owner_event":
        events = ", ".join(item["data"]["event_ids"])
        text += (
            f"Проверенное входящее владельца, receipt_id={item['id']}, event_ids={events}. "
            "Через work Telegram owner-event-read --event-id прочитай каждое точное событие и выполни поручение "
            "владельца по его смыслу. Ответ отправь владельцу через notify-owner с независимой проверкой. "
            f"Для голосового используй {root}/scripts/run-work-telegram-monitor-voice.sh --event-id EVENT --owner; "
            "нужны transcript и cleanup_verified=true. "
            "Транспортный job уже завершён routed: не используй его lease, job-result или клиентский send. "
            "Для разрешений связывай решение с конкретным action/target/version и исходным вопросом. "
        )
        if not state["duty_active"]:
            text += (
                "Оба режима проверяй отдельно. Твоё дежурство выключено: выполни только это разовое поручение, "
                "не создавай цель и не включай дежурство без явной команды владельца в сообщении. "
            )
    elif item:
        text += (
            f"Обновлена задача {item['data']['task_id']}. Перечитай её через manager_roles tasks. "
            "Срочное поручение возьми после ближайшей законченной операции; обычное — после текущей небольшой итерации. "
            "Прямое поручение владельца выше задач M1, задачи M1 выше самостоятельных улучшений. "
        )
    else:
        text += (
            "Дежурство включено владельцем. Продолжай постоянную цель. M1 ведёт сервис и ставит технические "
            "задачи M2; M2 сначала берёт задачи очереди. Если M2 уже дежурит, M1 не дублирует его исправления. "
            "Выпуск и перезапуск требуют конкретного действующего разрешения владельца. "
        )
    text += "Перед окончанием текущей итерации сохрани краткий checkpoint и проверенный результат."
    return text


class RoleExecutor:
    def __init__(self, store: RoleStore | None = None, *, rpc_factory: Any = role_rpc) -> None:
        self.store = store or RoleStore()
        self.rpc_factory = rpc_factory
        self.wake_event = asyncio.Event()

    async def request_reconcile(self, _claim: Any) -> dict[str, Any]:
        """A scheduler job only requests a bounded role pass, never awaits a goal."""
        self.wake_event.set()
        return {"ok": True, "result_code": "role_reconcile_requested"}

    async def tick(self) -> None:
        """One small reconciliation, protected across daemon/process instances."""
        self.store.project_automation_jobs()
        candidates = [role for role in ROLES if self._needed(role)]
        if not candidates:
            return
        self.store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.store.path.with_suffix(".dispatch.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            async with self.rpc_factory() as rpc:
                for role in candidates:
                    try:
                        await self._role_tick(role, rpc)
                    except RoleError as exc:
                        self.store.runtime_update(role, state="attention", last_error=str(exc))
                    except (OSError, TimeoutError):
                        self.store.runtime_update(role, last_error="role_reconcile_unavailable")
        except Exception:  # noqa: BLE001 - no RPC payloads, source text or credentials in durable diagnostics.
            for role in candidates:
                self.store.runtime_update(role, last_error="role_transport_unavailable")
        finally:
            os.close(fd)
            self.store.project_automation_jobs()

    def _needed(self, role: str) -> bool:
        state, dispatch, owner = self.store.dispatch_state(role)
        return bool(
            state["thread_id"]
            and (
                state["duty_active"] or state["goal_owned"] or dispatch or owner or self.store.pending_dispatches(role)
            )
        )

    @staticmethod
    async def _turns(
        rpc: Any, thread: str, *, full: bool = False, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        result = await rpc.request(
            "thread/turns/list",
            {
                "threadId": thread,
                "itemsView": "full" if full else "notLoaded",
                "sortDirection": "desc",
                "limit": 30,
                "cursor": cursor,
            },
        )
        turns = result.get("data")
        if not isinstance(turns, list) or any(
            not isinstance(turn, dict) or not isinstance(turn.get("id"), str) for turn in turns
        ):
            raise RoleError("role_turn_metadata_invalid")
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor):
            raise RoleError("role_turn_cursor_invalid")
        return turns, next_cursor

    async def _reconcile(self, role: str, rpc: Any, turns: list[dict[str, Any]], next_cursor: str | None) -> None:
        pending = self.store.pending_dispatches(role)
        for dispatch in pending:
            match = next((turn for turn in turns if turn["id"] == dispatch["turn_id"]), None)
            if dispatch["turn_id"] and match is None:
                cursor = dispatch.get("scan_cursor") or next_cursor
                page, more = await self._turns(rpc, dispatch["thread_id"], cursor=cursor) if cursor else ([], None)
                match = next((turn for turn in page if turn["id"] == dispatch["turn_id"]), None)
                if match is None:
                    if more:
                        self.store.advance_dispatch_scan(dispatch["id"], more)
                    else:
                        self.store.mark_dispatch(dispatch["id"], "unknown", error="role_turn_missing")
                    continue
            if not dispatch["turn_id"]:
                full, more = await self._turns(
                    rpc, dispatch["thread_id"], full=True, cursor=dispatch.get("scan_cursor")
                )
                found = [
                    turn
                    for turn in full
                    if any(
                        item.get("type") == "userMessage" and item.get("clientId") == dispatch["id"]
                        for item in turn.get("items", [])
                    )
                ]
                if len(found) == 1:
                    match = found[0]
                    self.store.mark_dispatch(dispatch["id"], "running", turn_id=match["id"])
                else:
                    if more and not any(turn["id"] == dispatch["before_turn_id"] for turn in full):
                        self.store.advance_dispatch_scan(dispatch["id"], more)
                    self.store.mark_dispatch(dispatch["id"], "unknown", error="role_launch_unknown")
                    continue
            if match and match.get("status") == "completed":
                self.store.mark_dispatch(dispatch["id"], "completed")
            elif match and match.get("status") in {"failed", "interrupted"}:
                self.store.mark_dispatch(dispatch["id"], "failed", error="role_turn_failed")

    async def _sync_goal(self, state: dict[str, Any], rpc: Any, *, allow_create: bool) -> bool:
        result = await rpc.request("thread/goal/get", {"threadId": state["thread_id"]})
        goal = result.get("goal")
        if goal is not None and not isinstance(goal, dict):
            raise RoleError("role_goal_invalid")
        if goal and goal.get("status") in {"usageLimited", "budgetLimited", "blocked"}:
            self.store.runtime_update(state["role"], state="attention", last_error="role_goal_limited_or_blocked")
            return False
        if goal and goal.get("objective") != role_objective(state["role"]):
            self.store.runtime_update(state["role"], state="attention", last_error="role_foreign_goal")
            return False
        sleeping = bool(state["state"] == "waiting" and state["next_wake_at"] and state["next_wake_at"] > now())
        sleeping = sleeping or self.store.maintenance_held()
        desired = "active" if state["duty_active"] and not sleeping else "paused"
        if not goal and (not allow_create or not state["duty_active"]):
            return True
        if goal and goal.get("status") in {"paused", "complete"} and state["goal_applied"] == "active":
            # An external pause/stop must never be undone by the scheduler.
            self.store.runtime_update(state["role"], state="attention", last_error="role_goal_manually_stopped")
            return False
        if not goal or goal.get("status") != desired:
            fresh = self.store.role_state(state["role"])
            if fresh["generation"] != state["generation"] or fresh["revision"] != state["revision"]:
                return False
            params = {
                "threadId": state["thread_id"],
                "status": desired,
                "origin": "user" if state["goal_applied"] is None or not state["duty_active"] else "automatic",
            }
            if not goal:
                params["objective"] = role_objective(state["role"])
            await rpc.request("thread/goal/set", params)
            fresh = self.store.role_state(state["role"])
            if desired == "active" and (fresh["generation"] != state["generation"] or not fresh["duty_active"]):
                await rpc.request(
                    "thread/goal/set", {"threadId": state["thread_id"], "status": "paused", "origin": "user"}
                )
                self.store.runtime_update(state["role"], goal_owned=False, goal_applied="paused")
                return False
        self.store.runtime_update(
            state["role"], goal_owned=state["duty_active"] or state["goal_owned"], goal_applied=desired
        )
        return True

    async def _role_tick(self, role: str, rpc: Any) -> None:
        state, _, owner = self.store.dispatch_state(role)
        if state["last_error"] in {
            "role_rpc_rejected",
            "role_turn_rejected",
            "role_host_mismatch",
            "role_thread_binding_invalid",
            "role_model_mismatch",
            "role_foreign_goal",
        }:
            return
        if state["host_id"] != executor_host_id():
            raise RoleError("role_host_mismatch")
        resumed = await rpc.request(
            "thread/resume", {"threadId": state["thread_id"], "cwd": PROJECT_DIR, "excludeTurns": True}
        )
        thread = resumed.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != state["thread_id"]
            or thread.get("cwd") != PROJECT_DIR
            or thread.get("ephemeral")
        ):
            raise RoleError("role_thread_binding_invalid")
        if resumed.get("model") != MODEL:
            raise RoleError("role_model_mismatch")
        turns, cursor = await self._turns(rpc, state["thread_id"])
        await self._reconcile(role, rpc, turns, cursor)
        state, dispatch, owner = self.store.dispatch_state(role)
        active = next((turn for turn in turns if turn.get("status") == "inProgress"), None)
        goal_ready = await self._sync_goal(state, rpc, allow_create=bool(dispatch))
        # Native goal continuations may have no controller dispatch row.
        if not state["duty_active"] and (state["goal_owned"] or (dispatch and dispatch["kind"] == "duty")) and active:
            await rpc.request("turn/interrupt", {"threadId": state["thread_id"], "turnId": active["id"]})
            return
        if not state["duty_active"] and state["goal_owned"] and not active:
            self.store.runtime_update(role, goal_owned=False)
        if not goal_ready:
            if owner and self.store.role_state(role)["last_error"] == "role_goal_manually_stopped":
                await self._dispatch({**state, "duty_active": False}, owner, rpc, active, turns)
            return
        if self.store.maintenance_held():
            return
        if any(item["status"] in {"starting", "unknown"} for item in self.store.pending_dispatches(role)):
            return
        if owner:
            await self._dispatch(state, owner, rpc, active, turns)
            return
        if not state["duty_active"] or active or dispatch:
            return
        if state["state"] == "waiting" and state["next_wake_at"] and state["next_wake_at"] > now():
            return
        await self._dispatch(state, None, rpc, None, turns)

    async def _dispatch(
        self,
        state: dict[str, Any],
        item: dict[str, Any] | None,
        rpc: Any,
        active: dict[str, Any] | None,
        turns: list[dict[str, Any]],
    ) -> None:
        # Validate instructions before recording a possibly-executed launch.
        instruction_bundle(state, item)
        dispatch = self.store.prepare_dispatch(
            state["role"],
            revision=state["revision"],
            generation=state["generation"],
            item_id=item["id"] if item else None,
            before_turn_id=turns[0]["id"] if turns else None,
            steer=active is not None,
        )
        prompt = dispatch_prompt(state, dispatch, item)
        params = {
            "threadId": state["thread_id"],
            "input": [{"type": "text", "text": prompt}],
            "clientUserMessageId": dispatch["id"],
        }
        if active:
            params["expectedTurnId"] = active["id"]
            method = "turn/steer"
        else:
            params.update(model=MODEL, cwd=PROJECT_DIR)
            method = "turn/start"
        try:
            result = await rpc.request(method, params)
            turn_id = result.get("turnId") if active else (result.get("turn") or {}).get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise RoleError("role_turn_start_invalid")
            self.store.mark_dispatch(dispatch["id"], "running", turn_id=turn_id)
        except RoleRpcRejected:
            self.store.mark_dispatch(dispatch["id"], "failed", error="role_turn_rejected")
            return
        except Exception:  # noqa: BLE001 - an unknown send is reconciled by exact clientUserMessageId, never replayed.
            self.store.mark_dispatch(dispatch["id"], "unknown", error="role_launch_unknown")
            return
        if state["duty_active"]:
            await self._sync_goal(self.store.role_state(state["role"]), rpc, allow_create=True)
