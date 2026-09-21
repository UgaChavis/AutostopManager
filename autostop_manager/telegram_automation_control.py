"""Strict owner-chat adapter for the local automation control protocol.

The adapter never reads Telegram itself and never sends a message. A coordinator
must pass one already resolved private message and may use ``reply_text`` only
after the normal guarded Telegram send flow.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .automation_control import AutomationControlClient
from .automation_registry import AutomationError


MAX_COMMAND_CHARS = 512
CONFIRMATION_TTL_SECONDS = 5 * 60
MAX_PENDING_CONFIRMATIONS = 32
_STATUS_COMMANDS = frozenset({"/automations", "регламентные задания", "покажи регламентные задания"})
_TOGGLE_PATTERN = re.compile(
    r"^(?:(?P<russian>включи|выключи)(?:\s+задание)?|(?P<english>on|off))\s+(?P<target>[^\r\n]{1,96})$",
    re.IGNORECASE,
)
_SCHEDULE_PATTERN = re.compile(
    r"^(?:поставь|измени\s+период|период)(?:\s+задания)?\s+(?P<target>[^\r\n]{1,96}?)\s+"
    r"(?:каждые|раз\s+в)\s+(?P<amount>[1-9][0-9]{0,3})\s*"
    r"(?P<unit>мин(?:ут(?:у|ы)?)?|час(?:а|ов)?)$",
    re.IGNORECASE,
)
_CREATE_PATTERN = re.compile(
    r"^добавь(?:\s+задание)?\s+(?P<template>[^\r\n]{1,96}?)"
    r"(?:\s+(?:каждые|раз\s+в)\s+(?P<amount>[1-9][0-9]{0,3})\s*"
    r"(?P<unit>мин(?:ут(?:у|ы)?)?|час(?:а|ов)?))?$",
    re.IGNORECASE,
)
_TIMEZONE_PATTERN = re.compile(
    r"^(?:часовой\s+пояс|timezone)(?:\s+задания)?\s+(?P<target>[^\r\n]{1,96}?)\s+"
    r"(?P<timezone>UTC|[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+){1,3})$",
    re.IGNORECASE,
)
_ACTIVE_WINDOW_PATTERN = re.compile(
    r"^(?:активные\s+часы|активное\s+окно)(?:\s+задания)?\s+(?P<target>[^\r\n]{1,96}?)\s+"
    r"(?:(?P<always>24/7)|(?P<start>(?:[01][0-9]|2[0-3]):[0-5][0-9])\s*[-–—]\s*"
    r"(?P<end>(?:[01][0-9]|2[0-3]):[0-5][0-9]))$",
    re.IGNORECASE,
)
_CONFIRM_PATTERN = re.compile(r"^(?:подтвердить|подтверждаю)\s+(?P<token>[A-F0-9]{8})$", re.IGNORECASE)
_CANCEL_PATTERN = re.compile(r"^отмена\s+(?P<token>[A-F0-9]{8})$", re.IGNORECASE)


class AutomationControl(Protocol):
    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TelegramAutomationMessage:
    peer_id: int
    message_id: int
    text: str
    is_private: bool


@dataclass(frozen=True)
class TelegramAutomationResult:
    handled: bool
    ok: bool
    action: str
    reply_text: str


@dataclass(frozen=True)
class _PendingConfirmation:
    token: str
    peer_id: int
    operation: str
    payload: dict[str, Any]
    expected_revision: int | None
    idempotency_key: str
    expires_at: float


def _normalized(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _minutes(amount: str, unit: str) -> int:
    value = int(amount)
    return value * 60 if unit.casefold().startswith("час") else value


def _response_data(response: dict[str, Any]) -> dict[str, Any] | None:
    data = response.get("data")
    return data if response.get("ok") is True and isinstance(data, dict) else None


def _error_code(response: dict[str, Any]) -> str:
    error = response.get("error")
    return (
        str(error.get("code") or "automation_control_failed")
        if isinstance(error, dict)
        else "automation_control_failed"
    )


class TelegramAutomationAdapter:
    """Map a small Russian command grammar to guarded automation operations."""

    def __init__(
        self,
        *,
        owner_peer_id: int,
        control: AutomationControl,
        confirmation_ttl_seconds: int = CONFIRMATION_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
        idempotency_secret: bytes | None = None,
    ) -> None:
        if type(owner_peer_id) is not int or owner_peer_id <= 0:
            raise ValueError("owner_peer_id_invalid")
        if not 30 <= confirmation_ttl_seconds <= 15 * 60:
            raise ValueError("confirmation_ttl_invalid")
        self.owner_peer_id = owner_peer_id
        self.control = control
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self._now = now
        self._idempotency_secret = secrets.token_bytes(32) if idempotency_secret is None else idempotency_secret
        if not isinstance(self._idempotency_secret, bytes) or len(self._idempotency_secret) != 32:
            raise ValueError("idempotency_secret_invalid")
        self._pending: dict[str, _PendingConfirmation] = {}

    def handle(self, message: TelegramAutomationMessage) -> TelegramAutomationResult:
        if (
            message.is_private is not True
            or type(message.peer_id) is not int
            or message.peer_id != self.owner_peer_id
            or type(message.message_id) is not int
            or message.message_id <= 0
            or not isinstance(message.text, str)
        ):
            return TelegramAutomationResult(False, False, "ignored", "")
        text = message.text.strip()
        if (
            not text
            or len(text) > MAX_COMMAND_CHARS
            or any(ord(char) < 32 and char not in "\t" for char in text)
            or "\n" in text
            or "\r" in text
        ):
            return TelegramAutomationResult(False, False, "ignored", "")
        self._expire_confirmations()
        normalized = _normalized(text)
        if normalized in _STATUS_COMMANDS:
            return self._status()
        if match := _CONFIRM_PATTERN.fullmatch(text):
            return self._confirm(match.group("token").upper(), message)
        if match := _CANCEL_PATTERN.fullmatch(text):
            token = match.group("token").upper()
            pending = self._pending.get(token)
            if pending is None or pending.peer_id != message.peer_id:
                return TelegramAutomationResult(True, False, "cancel", "Подтверждение не найдено или уже истекло.")
            del self._pending[token]
            return TelegramAutomationResult(True, True, "cancel", "Изменение отменено.")
        if match := _TOGGLE_PATTERN.fullmatch(text):
            enabled = (match.group("russian") or match.group("english")).casefold() in {"включи", "on"}
            return self._toggle(match.group("target"), enabled=enabled, message=message)
        if match := _SCHEDULE_PATTERN.fullmatch(text):
            return self._preview_schedule(
                match.group("target"),
                every_minutes=_minutes(match.group("amount"), match.group("unit")),
                message=message,
            )
        if match := _TIMEZONE_PATTERN.fullmatch(text):
            timezone = match.group("timezone")
            if timezone.casefold() == "utc":
                timezone = "UTC"
            return self._preview_schedule_patch(
                match.group("target"),
                changes={"timezone": timezone},
                message=message,
                summary=f"Часовой пояс: {timezone}.",
            )
        if match := _ACTIVE_WINDOW_PATTERN.fullmatch(text):
            active_window: str | dict[str, str]
            if match.group("always"):
                active_window = "24/7"
                label = "24/7"
            else:
                active_window = {"start": match.group("start"), "end": match.group("end")}
                label = f"{match.group('start')}–{match.group('end')}"
            return self._preview_schedule_patch(
                match.group("target"),
                changes={"active_window": active_window},
                message=message,
                summary=f"Активные часы: {label}.",
            )
        if match := _CREATE_PATTERN.fullmatch(text):
            every_minutes = _minutes(match.group("amount"), match.group("unit")) if match.group("amount") else None
            return self._preview_create(match.group("template"), every_minutes=every_minutes, message=message)
        return TelegramAutomationResult(False, False, "ignored", "")

    def _status(self) -> TelegramAutomationResult:
        response = self._request("status")
        data = _response_data(response)
        if data is None:
            return self._failure("status", response)
        jobs = data.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            return TelegramAutomationResult(True, True, "status", "Регламентных заданий пока нет.")
        lines = []
        for job in jobs[:20]:
            if not isinstance(job, dict):
                continue
            state = "ON" if job.get("desired_state") == "on" else "OFF"
            actual_state = str(job.get("actual_state") or "unknown")
            schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
            period = schedule.get("every_minutes")
            timezone = schedule.get("timezone") or "не задан"
            active_window = schedule.get("active_window")
            if isinstance(active_window, dict):
                active_window_label = f"{active_window.get('start')}–{active_window.get('end')}"
            else:
                active_window_label = str(active_window or "не задано")
            lines.append(
                f"{job.get('name') or job.get('job_id')}: {state} ({actual_state}), {period} мин., "
                f"{timezone}, {active_window_label}."
            )
        return TelegramAutomationResult(True, True, "status", "\n".join(lines) or "Нет доступных заданий.")

    def _jobs(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        response = self._request("status")
        data = _response_data(response)
        if data is None or not isinstance(data.get("jobs"), list):
            return [], response
        return [job for job in data["jobs"] if isinstance(job, dict)], None

    def _resolve_job(self, target: str) -> tuple[dict[str, Any] | None, str | None]:
        jobs, failure = self._jobs()
        if failure is not None:
            return None, "Состояние заданий сейчас недоступно; ничего не изменено."
        wanted = _normalized(target)
        matches = {
            str(job.get("job_id")): job
            for job in jobs
            if wanted in {_normalized(str(job.get("job_id") or "")), _normalized(str(job.get("name") or ""))}
        }
        if not matches:
            return None, "Точное задание не найдено; ничего не изменено."
        if len(matches) != 1:
            return None, "Название неоднозначно. Укажите точный ID задания."
        return next(iter(matches.values())), None

    def _resolve_template(self, target: str) -> tuple[dict[str, Any] | None, str | None]:
        response = self._request("templates")
        data = _response_data(response)
        templates = data.get("templates") if data is not None else None
        if not isinstance(templates, list):
            return None, "Шаблоны сейчас недоступны; ничего не создано."
        wanted = _normalized(target)
        matches = {
            str(template.get("template_id")): template
            for template in templates
            if isinstance(template, dict)
            and wanted
            in {
                _normalized(str(template.get("template_id") or "")),
                _normalized(str(template.get("name") or "")),
            }
        }
        if not matches:
            return None, "Точный шаблон не найден; ничего не создано."
        if len(matches) != 1:
            return None, "Шаблон неоднозначен. Укажите точный template ID."
        return next(iter(matches.values())), None

    def _toggle(self, target: str, *, enabled: bool, message: TelegramAutomationMessage) -> TelegramAutomationResult:
        job, error = self._resolve_job(target)
        if job is None:
            return TelegramAutomationResult(True, False, "set_enabled", str(error))
        job_id = str(job.get("job_id") or "")
        revision = job.get("revision")
        if type(revision) is not int:
            return TelegramAutomationResult(
                True, False, "set_enabled", "Ревизия задания недоступна; ничего не изменено."
            )
        expected = "on" if enabled else "off"
        if job.get("desired_state") == expected:
            actual_state = str(job.get("actual_state") or "unknown")
            next_run = job.get("next_run_at") or "не назначен"
            return TelegramAutomationResult(
                True,
                True,
                "set_enabled",
                f"{job.get('name') or job_id}: уже {expected.upper()}; фактически: {actual_state}; "
                f"следующий запуск: {next_run}.",
            )
        response = self._request(
            "set_enabled",
            {"job_id": job_id, "enabled": enabled},
            idempotency_key=self._idempotency_key(message, "set-enabled", job_id),
            expected_revision=revision,
        )
        if _response_data(response) is None:
            return self._failure("set_enabled", response)
        readback = self._request("status", {"job_id": job_id})
        data = _response_data(readback)
        jobs = data.get("jobs") if data is not None else None
        if (
            not isinstance(jobs, list)
            or len(jobs) != 1
            or jobs[0].get("job_id") != job_id
            or jobs[0].get("desired_state") != expected
        ):
            return TelegramAutomationResult(
                True,
                False,
                "set_enabled",
                "Команда принята, но точное состояние не подтверждено. Повтор не выполнялся.",
            )
        verified = jobs[0]
        actual_state = str(verified.get("actual_state") or "unknown")
        next_run = verified.get("next_run_at") or "не назначен"
        return TelegramAutomationResult(
            True,
            True,
            "set_enabled",
            f"{verified.get('name') or job_id}: {expected.upper()}; фактически: {actual_state}; "
            f"следующий запуск: {next_run}.",
        )

    def _preview_schedule(
        self, target: str, *, every_minutes: int, message: TelegramAutomationMessage
    ) -> TelegramAutomationResult:
        job, error = self._resolve_job(target)
        if job is None:
            return TelegramAutomationResult(True, False, "set_schedule", str(error))
        return self._preview_schedule_job(
            job,
            changes={"every_minutes": every_minutes},
            message=message,
            summary=f"Период: {every_minutes} мин.",
        )

    def _preview_schedule_patch(
        self,
        target: str,
        *,
        changes: dict[str, Any],
        message: TelegramAutomationMessage,
        summary: str,
    ) -> TelegramAutomationResult:
        job, error = self._resolve_job(target)
        if job is None:
            return TelegramAutomationResult(True, False, "set_schedule", str(error))
        return self._preview_schedule_job(job, changes=changes, message=message, summary=summary)

    def _preview_schedule_job(
        self,
        job: dict[str, Any],
        *,
        changes: dict[str, Any],
        message: TelegramAutomationMessage,
        summary: str,
    ) -> TelegramAutomationResult:
        revision = job.get("revision")
        schedule = job.get("schedule")
        if type(revision) is not int or not isinstance(schedule, dict):
            return TelegramAutomationResult(
                True, False, "set_schedule", "Текущее расписание или ревизия недоступны; ничего не изменено."
            )
        merged_schedule = copy.deepcopy(schedule)
        merged_schedule.update(copy.deepcopy(changes))
        payload = {"job_id": str(job["job_id"]), "schedule": merged_schedule}
        return self._preview(
            operation="set_schedule",
            payload=payload,
            expected_revision=revision,
            message=message,
            summary=f"Расписание {job.get('name') or job['job_id']}. {summary}",
        )

    def _preview_create(
        self, target: str, *, every_minutes: int | None, message: TelegramAutomationMessage
    ) -> TelegramAutomationResult:
        template, error = self._resolve_template(target)
        if template is None:
            return TelegramAutomationResult(True, False, "create_from_template", str(error))
        payload: dict[str, Any] = {"template_id": str(template["template_id"]), "enabled": False}
        if every_minutes is not None:
            payload["schedule"] = {"every_minutes": every_minutes}
        return self._preview(
            operation="create_from_template",
            payload=payload,
            expected_revision=None,
            message=message,
            summary=f"Создать {template.get('name') or template['template_id']} в состоянии OFF.",
        )

    def _preview(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        expected_revision: int | None,
        message: TelegramAutomationMessage,
        summary: str,
    ) -> TelegramAutomationResult:
        response = self._request(
            "preview",
            {"target_operation": operation, "target_payload": payload},
        )
        if _response_data(response) is None:
            return self._failure(operation, response)
        if len(self._pending) >= MAX_PENDING_CONFIRMATIONS:
            oldest = min(self._pending.values(), key=lambda item: item.expires_at)
            self._pending.pop(oldest.token, None)
        token = self._new_confirmation_token()
        self._pending[token] = _PendingConfirmation(
            token=token,
            peer_id=message.peer_id,
            operation=operation,
            payload=payload,
            expected_revision=expected_revision,
            idempotency_key=self._idempotency_key(
                message, operation, str(payload.get("job_id") or payload.get("template_id"))
            ),
            expires_at=float(self._now()) + self.confirmation_ttl_seconds,
        )
        return TelegramAutomationResult(
            True,
            True,
            f"preview_{operation}",
            f"Предпросмотр: {summary} Для применения: Подтвердить {token}",
        )

    def _confirm(self, token: str, message: TelegramAutomationMessage) -> TelegramAutomationResult:
        pending = self._pending.get(token)
        if pending is None or pending.peer_id != message.peer_id:
            return TelegramAutomationResult(True, False, "confirm", "Подтверждение не найдено или уже истекло.")
        response = self._request(
            pending.operation,
            pending.payload,
            idempotency_key=pending.idempotency_key,
            expected_revision=pending.expected_revision,
        )
        data = _response_data(response)
        if data is None:
            if _error_code(response) == "automation_revision_conflict":
                del self._pending[token]
                return TelegramAutomationResult(
                    True, False, pending.operation, "Задание уже изменилось. Предпросмотр отменён; запросите новый."
                )
            return self._failure(pending.operation, response)
        job = data.get("job") if isinstance(data.get("job"), dict) else None
        if job is None or not job.get("job_id"):
            del self._pending[token]
            return TelegramAutomationResult(
                True,
                False,
                pending.operation,
                "Изменение принято, но идентификатор для readback не получен. Повтор не выполнялся.",
            )
        job_id = str(job["job_id"])
        if pending.operation == "create_from_template" and job.get("desired_state") != "off":
            del self._pending[token]
            return TelegramAutomationResult(
                True, False, pending.operation, "Создание не подтверждено в безопасном состоянии OFF."
            )
        readback = self._request("status", {"job_id": job_id})
        readback_data = _response_data(readback)
        jobs = readback_data.get("jobs") if readback_data is not None else None
        if not isinstance(jobs, list) or len(jobs) != 1 or jobs[0].get("job_id") != job_id:
            del self._pending[token]
            return TelegramAutomationResult(
                True,
                False,
                pending.operation,
                "Изменение применено, но точный readback не подтверждён. Повтор не выполнялся.",
            )
        job = jobs[0]
        del self._pending[token]
        if pending.operation == "set_schedule":
            schedule = job.get("schedule") if isinstance(job, dict) and isinstance(job.get("schedule"), dict) else {}
            expected_schedule = pending.payload["schedule"]
            if any(schedule.get(key) != value for key, value in expected_schedule.items()):
                return TelegramAutomationResult(
                    True, False, pending.operation, "Изменение применено, но новое расписание не подтверждено."
                )
            reply = "Расписание применено и подтверждено."
        elif pending.operation == "create_from_template":
            expected_schedule = pending.payload.get("schedule")
            schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
            if (
                job.get("desired_state") != "off"
                or job.get("template_id") != pending.payload["template_id"]
                or (
                    isinstance(expected_schedule, dict)
                    and any(schedule.get(key) != value for key, value in expected_schedule.items())
                )
            ):
                return TelegramAutomationResult(
                    True,
                    False,
                    pending.operation,
                    "Задание создано, но безопасное состояние OFF и период не подтверждены.",
                )
            reply = f"Задание создано в состоянии OFF: {job.get('name')}."
        else:
            reply = "Изменение применено и проверено."
        return TelegramAutomationResult(True, True, pending.operation, reply)

    def _failure(self, action: str, response: dict[str, Any]) -> TelegramAutomationResult:
        code = _error_code(response)
        return TelegramAutomationResult(True, False, action, f"Не выполнено ({code}); состояние не подтверждено.")

    def _request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self.control.request(
                operation,
                payload,
                idempotency_key=idempotency_key,
                expected_revision=expected_revision,
            )
        except (AutomationError, OSError, TimeoutError):
            return {"ok": False, "error": {"code": "automation_control_unavailable"}}

    def _expire_confirmations(self) -> None:
        now = float(self._now())
        self._pending = {token: pending for token, pending in self._pending.items() if pending.expires_at >= now}
        if len(self._pending) > MAX_PENDING_CONFIRMATIONS:
            oldest = sorted(self._pending.values(), key=lambda item: item.expires_at)
            for pending in oldest[: len(self._pending) - MAX_PENDING_CONFIRMATIONS]:
                self._pending.pop(pending.token, None)

    def _new_confirmation_token(self) -> str:
        for _ in range(10):
            token = secrets.token_hex(4).upper()
            if token not in self._pending:
                return token
        raise RuntimeError("confirmation_token_unavailable")

    def _idempotency_key(self, message: TelegramAutomationMessage, operation: str, target: str) -> str:
        digest = hmac.new(
            self._idempotency_secret,
            f"telegram-owner-command-v1\0{message.peer_id}\0{message.message_id}\0{operation}\0{target}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"tg-auto:{digest[:40]}"


class TelegramAutomationIncomingCoordinator:
    """Consume only bounded owner commands; leave all other text to Codex wake."""

    def __init__(self, adapter: TelegramAutomationAdapter, *, replay_cache_size: int = 32) -> None:
        if not 1 <= replay_cache_size <= 128:
            raise ValueError("automation_replay_cache_size_invalid")
        self.adapter = adapter
        self.replay_cache_size = replay_cache_size
        self._handled_replies: OrderedDict[int, str] = OrderedDict()
        self._route_lock = asyncio.Lock()

    async def route(
        self,
        message: TelegramAutomationMessage,
        *,
        reply: Callable[[str, int], Awaitable[bool]],
    ) -> bool:
        if (
            message.is_private is not True
            or type(message.peer_id) is not int
            or message.peer_id != self.adapter.owner_peer_id
            or type(message.message_id) is not int
            or message.message_id <= 0
        ):
            return False
        async with self._route_lock:
            cached_reply = self._handled_replies.get(message.message_id)
            if cached_reply is not None:
                self._handled_replies.move_to_end(message.message_id)
                with suppress(OSError, TimeoutError):
                    await reply(cached_reply, message.message_id)
                return True
            result = await asyncio.to_thread(self.adapter.handle, message)
            if not result.handled:
                return False
            self._handled_replies[message.message_id] = result.reply_text
            while len(self._handled_replies) > self.replay_cache_size:
                self._handled_replies.popitem(last=False)
            with suppress(OSError, TimeoutError):
                await reply(result.reply_text, message.message_id)
            return True


def build_runtime_owner_adapter(
    *,
    owner_peer_id: int,
    socket_path: Path | None = None,
    idempotency_secret: bytes | None = None,
) -> TelegramAutomationAdapter:
    client = AutomationControlClient(
        socket_path=socket_path,
        actor={"kind": "telegram_owner", "id": "owner-command-adapter-v1", "is_admin": False},
    )
    return TelegramAutomationAdapter(
        owner_peer_id=owner_peer_id,
        control=client,
        idempotency_secret=idempotency_secret,
    )
