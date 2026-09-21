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
from typing import Any, Protocol, cast

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


@dataclass(frozen=True)
class _ResolvedTarget:
    kind: str
    item: dict[str, Any]


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
        timers = data.get("system_timers", [])
        if not isinstance(jobs, list) or not isinstance(timers, list):
            return TelegramAutomationResult(
                True,
                False,
                "status",
                "Состояние регламентных заданий сейчас недоступно.",
            )
        lines: list[str] = []
        for job in jobs:
            # Reserve room for the small reviewed system-timer allowlist.
            if len(lines) >= 15:
                break
            if not isinstance(job, dict):
                continue
            state = "ON" if job.get("desired_state") == "on" else "OFF"
            actual_state = str(job.get("actual_state") or "unknown")
            raw_schedule = job.get("schedule")
            schedule: Mapping[str, Any] = raw_schedule if isinstance(raw_schedule, dict) else {}
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
        for timer in timers:
            if len(lines) >= 20:
                break
            if not isinstance(timer, dict):
                continue
            state = "ON" if timer.get("desired_state") == "on" else "OFF"
            actual_state = str(timer.get("actual_state") or "unknown")
            period = timer.get("period_minutes")
            access = "только чтение" if timer.get("locked") is True else "управляемый"
            lines.append(
                f"{timer.get('name') or timer.get('timer_id')} [таймер {timer.get('timer_id')}]: "
                f"{state} ({actual_state}), {period} мин., {access}."
            )
        return TelegramAutomationResult(
            True,
            True,
            "status",
            "\n".join(lines) or "Регламентных заданий и системных таймеров пока нет.",
        )

    def _targets(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
        response = self._request("status")
        data = _response_data(response)
        if (
            data is None
            or not isinstance(data.get("jobs"), list)
            or not isinstance(data.get("system_timers", []), list)
        ):
            return [], [], response
        jobs = [job for job in data["jobs"] if isinstance(job, dict)]
        timers = [timer for timer in data.get("system_timers", []) if isinstance(timer, dict)]
        return jobs, timers, None

    def _resolve_target(self, target: str) -> tuple[_ResolvedTarget | None, str | None]:
        jobs, timers, failure = self._targets()
        if failure is not None:
            return None, "Состояние заданий сейчас недоступно; ничего не изменено."
        wanted = _normalized(target)
        matches: dict[tuple[str, str], _ResolvedTarget] = {}
        for kind, identifier, items in (
            ("job", "job_id", jobs),
            ("system_timer", "timer_id", timers),
        ):
            for item in items:
                item_id = str(item.get(identifier) or "")
                if wanted in {_normalized(item_id), _normalized(str(item.get("name") or ""))}:
                    matches[(kind, item_id)] = _ResolvedTarget(kind=kind, item=item)
        if not matches:
            return None, "Точная автоматизация не найдена; ничего не изменено."
        if len(matches) != 1:
            return None, "Название неоднозначно. Укажите точный ID задания или таймера."
        return next(iter(matches.values())), None

    def _readback_target(self, kind: str, target_id: str) -> dict[str, Any] | None:
        payload = {"job_id": target_id} if kind == "job" else {}
        response = self._request("status", payload)
        data = _response_data(response)
        if data is None:
            return None
        collection_name = "jobs" if kind == "job" else "system_timers"
        identifier = "job_id" if kind == "job" else "timer_id"
        collection = data.get(collection_name)
        if not isinstance(collection, list):
            return None
        matches = [item for item in collection if isinstance(item, dict) and item.get(identifier) == target_id]
        return matches[0] if len(matches) == 1 else None

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
        resolved, error = self._resolve_target(target)
        if resolved is None:
            return TelegramAutomationResult(True, False, "set_enabled", str(error))
        item = resolved.item
        identifier = "job_id" if resolved.kind == "job" else "timer_id"
        target_id = str(item.get(identifier) or "")
        if resolved.kind == "system_timer" and (item.get("locked") is True or item.get("control_mode") != "managed"):
            return TelegramAutomationResult(
                True,
                False,
                "set_enabled",
                "Этот системный таймер доступен только для чтения; ничего не изменено.",
            )
        revision = item.get("revision")
        if type(revision) is not int or (resolved.kind == "system_timer" and revision < 1):
            return TelegramAutomationResult(
                True, False, "set_enabled", "Ревизия автоматизации недоступна; ничего не изменено."
            )
        expected = "on" if enabled else "off"
        expected_actual = "active" if enabled else "inactive"
        already_verified = item.get("desired_state") == expected
        if resolved.kind == "system_timer":
            already_verified = (
                already_verified
                and item.get("actual_state") == expected_actual
                and item.get("reconcile_state") == "in_sync"
            )
        if already_verified:
            actual_state = str(item.get("actual_state") or "unknown")
            next_run = item.get("next_run_at") or "не назначен"
            return TelegramAutomationResult(
                True,
                True,
                "set_enabled",
                f"{item.get('name') or target_id}: уже {expected.upper()}; фактически: {actual_state}; "
                f"следующий запуск: {next_run}.",
            )
        payload = {identifier: target_id, "enabled": enabled}
        response = self._request(
            "set_enabled",
            payload,
            idempotency_key=self._idempotency_key(message, f"set-enabled-{resolved.kind}", target_id),
            expected_revision=revision,
        )
        if _response_data(response) is None:
            return self._failure("set_enabled", response)
        verified = self._readback_target(resolved.kind, target_id)
        readback_matches = verified is not None and verified.get("desired_state") == expected
        if resolved.kind == "system_timer":
            readback_matches = bool(
                readback_matches
                and verified is not None
                and verified.get("actual_state") == expected_actual
                and verified.get("reconcile_state") == "in_sync"
            )
        if not readback_matches:
            return TelegramAutomationResult(
                True,
                False,
                "set_enabled",
                "Команда принята, но точное состояние не подтверждено. Повтор не выполнялся.",
            )
        verified_item = cast(dict[str, Any], verified)
        actual_state = str(verified_item.get("actual_state") or "unknown")
        next_run = verified_item.get("next_run_at") or "не назначен"
        return TelegramAutomationResult(
            True,
            True,
            "set_enabled",
            f"{verified_item.get('name') or target_id}: {expected.upper()}; фактически: {actual_state}; "
            f"следующий запуск: {next_run}.",
        )

    def _preview_schedule(
        self, target: str, *, every_minutes: int, message: TelegramAutomationMessage
    ) -> TelegramAutomationResult:
        resolved, error = self._resolve_target(target)
        if resolved is None:
            return TelegramAutomationResult(True, False, "set_schedule", str(error))
        if resolved.kind == "system_timer":
            timer = resolved.item
            if timer.get("locked") is True or timer.get("control_mode") != "managed":
                return TelegramAutomationResult(
                    True,
                    False,
                    "set_schedule",
                    "Этот системный таймер доступен только для чтения; ничего не изменено.",
                )
            revision = timer.get("revision")
            if type(revision) is not int or revision < 1:
                return TelegramAutomationResult(
                    True, False, "set_schedule", "Ревизия таймера недоступна; ничего не изменено."
                )
            timer_id = str(timer.get("timer_id") or "")
            return self._preview(
                operation="set_schedule",
                payload={"timer_id": timer_id, "schedule": {"every_minutes": every_minutes}},
                expected_revision=revision,
                message=message,
                summary=f"Период системного таймера {timer.get('name') or timer_id}: {every_minutes} мин.",
            )
        return self._preview_schedule_job(
            resolved.item,
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
        resolved, error = self._resolve_target(target)
        if resolved is None:
            return TelegramAutomationResult(True, False, "set_schedule", str(error))
        if resolved.kind != "job":
            return TelegramAutomationResult(
                True,
                False,
                "set_schedule",
                "Часовой пояс и активные часы применимы только к заданиям; ничего не изменено.",
            )
        return self._preview_schedule_job(resolved.item, changes=changes, message=message, summary=summary)

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
                message,
                operation,
                str(payload.get("job_id") or payload.get("timer_id") or payload.get("template_id")),
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
        pending_timer_id = pending.payload.get("timer_id")
        if isinstance(pending_timer_id, str):
            timer = data.get("system_timer") if isinstance(data.get("system_timer"), dict) else None
            if timer is None or timer.get("timer_id") != pending_timer_id:
                del self._pending[token]
                return TelegramAutomationResult(
                    True,
                    False,
                    pending.operation,
                    "Изменение принято, но таймер для readback не получен. Повтор не выполнялся.",
                )
            verified_timer = self._readback_target("system_timer", pending_timer_id)
            del self._pending[token]
            expected_minutes = pending.payload.get("schedule", {}).get("every_minutes")
            if (
                verified_timer is None
                or verified_timer.get("period_minutes") != expected_minutes
                or verified_timer.get("actual_period_minutes") != expected_minutes
                or verified_timer.get("reconcile_state") != "in_sync"
            ):
                return TelegramAutomationResult(
                    True,
                    False,
                    pending.operation,
                    "Изменение применено, но новый период таймера не подтверждён. Повтор не выполнялся.",
                )
            return TelegramAutomationResult(
                True,
                True,
                pending.operation,
                f"Период системного таймера подтверждён: {expected_minutes} мин.",
            )
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
            raw_schedule = job.get("schedule") if isinstance(job, dict) else None
            schedule: Mapping[str, Any] = raw_schedule if isinstance(raw_schedule, dict) else {}
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
