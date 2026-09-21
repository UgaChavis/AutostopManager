"""Persistent scheduler and AF_UNIX control daemon for Manager automations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, cast
from uuid import uuid4

from .automation_control import AutomationControlServer, AutomationControlService
from .automation_jobs import (
    AutomationJobError,
    CrmDigestExecutor,
    HttpCrmDigestSource,
    OwnerNotifier,
    TelegramOwnerNotifier,
)
from .automation_registry import AutomationStore
from .automation_timers import SystemTimerController
from .config import get_automation_crm_connection_config, get_work_telegram_socket_path, load_runtime_env


Executor = Callable[[Mapping[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
TEST_NOTIFICATION_TEXT = "Тест уведомлений: Центр автоматизаций подключён."
INCIDENT_ID_PATTERN = re.compile(r"inc_[0-9a-f]{32}\Z")
RESULT_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")


def notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    target = "\0" + address[1:] if address.startswith("@") else address
    with suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.connect(target)
        notifier.sendall(message.encode())


def notification_readiness(notifier: OwnerNotifier) -> dict[str, str]:
    """Probe the bridge without sending and return privacy-safe readiness checks."""

    try:
        status = notifier.status()
    except (AutomationJobError, OSError, TimeoutError):
        status = {}
    return {
        "notification_transport": "ready" if status.get("transport_ready") is True else "unavailable",
        "owner_notification_target": (
            "ready" if status.get("owner_notification_configured") is True else "not_configured"
        ),
    }


def crm_change_feed_readiness(source: HttpCrmDigestSource | None) -> str:
    """Probe authenticated CRM feed health without registering its consumer."""

    if source is None:
        return "not_configured"
    try:
        status = source.readiness()
    except (AutomationJobError, OSError, TimeoutError):
        return "unavailable"
    return "pending" if status.get("pending_publish") is True else "ready"


class AutomationDaemon:
    def __init__(
        self,
        *,
        store: AutomationStore | None = None,
        control_server: AutomationControlServer | None = None,
        executors: Mapping[str, Executor] | None = None,
        timer_controller: SystemTimerController | None = None,
        notifier: OwnerNotifier | None = None,
        readiness_provider: Callable[[], Mapping[str, Any]] | None = None,
        tick_seconds: float = 1,
        lease_seconds: int = 60,
    ) -> None:
        self.store = store or AutomationStore()
        self.executors = dict(executors or {})
        self.timer_controller = timer_controller or SystemTimerController(store=self.store)
        self.notifier = notifier
        self.readiness_provider = readiness_provider
        self.control_server = control_server or AutomationControlServer(
            service=AutomationControlService(
                self.store,
                timer_controller=self.timer_controller,
                readiness_provider=readiness_provider,
            )
        )
        self.tick_seconds = tick_seconds
        self.lease_seconds = lease_seconds
        self.owner = f"automation-daemon-{uuid4().hex}"
        self.stopping = asyncio.Event()

    @classmethod
    def production(cls, *, store: AutomationStore | None = None) -> AutomationDaemon:
        state = store or AutomationStore()
        crm_config = get_automation_crm_connection_config()
        telegram_socket = get_work_telegram_socket_path()
        notifier = TelegramOwnerNotifier(telegram_socket)
        executors: dict[str, Executor] = {}
        crm_ready = bool(crm_config.configured and not crm_config.error_code)
        crm_source: HttpCrmDigestSource | None = None
        crm_readiness_source: HttpCrmDigestSource | None = None
        if crm_ready:
            crm_source = HttpCrmDigestSource(crm_config)
            crm_readiness_source = HttpCrmDigestSource(crm_config, timeout_seconds=2)
            executors["crm_digest_v1"] = CrmDigestExecutor(
                store=state,
                source=crm_source,
                notifier=notifier,
            )

        def readiness() -> Mapping[str, Any]:
            checks = {
                "crm_digest_executor": "ready" if "crm_digest_v1" in executors else "not_configured",
                "crm_change_feed": (
                    crm_change_feed_readiness(crm_readiness_source)
                    if crm_ready
                    else (crm_config.error_code or "not_configured")
                ),
                **notification_readiness(notifier),
            }
            return {"ready": all(value == "ready" for value in checks.values()), "checks": checks}

        return cls(store=state, executors=executors, notifier=notifier, readiness_provider=readiness)

    async def _execute_claim(self, claim: Mapping[str, Any]) -> None:
        executor = self.executors.get(str(claim["template_id"]))
        succeeded = False
        result_code = "automation_executor_unavailable"
        outcome: Mapping[str, Any] = {}
        if executor is not None:
            try:
                invocation = {**claim, "lease_owner": self.owner}
                if inspect.iscoroutinefunction(executor):
                    outcome = await executor(invocation)
                else:
                    sync_executor = cast(Callable[[Mapping[str, Any]], Any], executor)
                    sync_outcome = await asyncio.to_thread(sync_executor, invocation)
                    outcome = await sync_outcome if inspect.isawaitable(sync_outcome) else sync_outcome
                succeeded = bool(outcome.get("ok"))
                raw_result_code = outcome.get("result_code")
                result_code = str(raw_result_code or ("ok" if succeeded else "automation_executor_failed"))
            except Exception:  # noqa: BLE001 - executor data and exception text must not enter durable state.
                result_code = "automation_executor_failed"
        if result_code == "automation_claim_superseded":
            self.store.cancel_run_claim(
                job_id=str(claim["job_id"]),
                run_id=str(claim["run_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
            )
        else:
            self.store.finish_run(
                job_id=str(claim["job_id"]),
                run_id=str(claim["run_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
                succeeded=succeeded,
                result_code=result_code,
                next_delay_seconds=(
                    int(outcome["next_delay_seconds"])
                    if executor is not None
                    and isinstance(outcome, Mapping)
                    and type(outcome.get("next_delay_seconds")) is int
                    else None
                ),
            )

    async def _execute_outbox(self, claim: Mapping[str, Any]) -> None:
        payload = claim.get("payload")
        text: str | None = None
        if claim.get("kind") == "notification_test" and isinstance(payload, Mapping):
            command_id = payload.get("command_id")
            if (
                set(payload) == {"template", "command_id"}
                and payload.get("template") == "automation_notification_test_v1"
                and isinstance(command_id, str)
                and len(command_id) == 36
                and command_id.startswith("cmd_")
            ):
                text = TEST_NOTIFICATION_TEXT
        elif claim.get("kind") in {"job_error_alert", "job_recovery_alert"} and isinstance(payload, Mapping):
            transition = "error" if claim.get("kind") == "job_error_alert" else "recovery"
            incident_id = payload.get("incident_id")
            error_code = payload.get("error_code")
            if (
                set(payload) == {"template", "template_id", "transition", "incident_id", "error_code"}
                and payload.get("template") == "automation_job_incident_v1"
                and payload.get("template_id") == "crm_digest_v1"
                and payload.get("transition") == transition
                and isinstance(incident_id, str)
                and INCIDENT_ID_PATTERN.fullmatch(incident_id)
                and isinstance(error_code, str)
                and RESULT_CODE_PATTERN.fullmatch(error_code)
            ):
                if transition == "error":
                    text = (
                        "Центр автоматизаций: CRM-дайджест перешёл в ERROR "
                        f"(код: {error_code}). Повторы этого инцидента подавлены."
                    )
                else:
                    text = "Центр автоматизаций: CRM-дайджест снова работает. Инцидент закрыт."
        if text is None:
            self.store.finish_outbox(
                outbox_id=str(claim["outbox_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
                sent=False,
                result_code="automation_outbox_payload_invalid",
            )
            return
        if self.notifier is None:
            self.store.finish_outbox(
                outbox_id=str(claim["outbox_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
                sent=False,
                result_code="automation_telegram_unavailable",
            )
            return
        expected_hash = hashlib.sha256(text.encode()).hexdigest()
        if claim.get("recovery_only") is True:
            try:
                lookup = await asyncio.to_thread(
                    self.notifier.lookup,
                    idempotency_key=str(claim["idempotency_key"]),
                    text_sha256=expected_hash,
                )
            except AutomationJobError:
                lookup = {}
            if (
                lookup.get("outcome") == "verified"
                and lookup.get("found") is True
                and lookup.get("verified") is True
                and lookup.get("text_sha256") == expected_hash
                and type(lookup.get("message_id")) is int
                and int(lookup["message_id"]) > 0
            ):
                self.store.finish_outbox(
                    outbox_id=str(claim["outbox_id"]),
                    owner=self.owner,
                    fencing_token=int(claim["fencing_token"]),
                    sent=True,
                    result_code="ok",
                )
            else:
                self.store.block_outbox_claim(
                    outbox_id=str(claim["outbox_id"]),
                    owner=self.owner,
                    fencing_token=int(claim["fencing_token"]),
                )
            return
        if not self.store.outbox_claim_is_current(
            outbox_id=str(claim["outbox_id"]),
            owner=self.owner,
            fencing_token=int(claim["fencing_token"]),
            claimed_revision=int(claim["claimed_revision"]),
        ):
            self.store.cancel_outbox_claim(
                outbox_id=str(claim["outbox_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
            )
            return
        try:
            preview = await asyncio.to_thread(self.notifier.preview, text)
            token = preview.get("contract_token")
            if (
                preview.get("mode") != "dry_run"
                or preview.get("text_sha256") != expected_hash
                or not isinstance(token, str)
                or not token
            ):
                raise AutomationJobError("automation_notification_preview_invalid")
            if not self.store.outbox_claim_is_current(
                outbox_id=str(claim["outbox_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
                claimed_revision=int(claim["claimed_revision"]),
            ):
                self.store.cancel_outbox_claim(
                    outbox_id=str(claim["outbox_id"]),
                    owner=self.owner,
                    fencing_token=int(claim["fencing_token"]),
                )
                return
            applied = await asyncio.to_thread(
                self.notifier.apply,
                text,
                contract_token=token,
                idempotency_key=str(claim["idempotency_key"]),
            )
            if (
                applied.get("verified") is not True
                or type(applied.get("message_id")) is not int
                or int(applied["message_id"]) <= 0
            ):
                raise AutomationJobError("automation_telegram_outcome_unknown", outcome_uncertain=True)
        except AutomationJobError as exc:
            if exc.outcome_uncertain:
                # The expired lease is later claimed in recovery-only mode and
                # resolved through the bridge's exact idempotency readback.
                return
            self.store.finish_outbox(
                outbox_id=str(claim["outbox_id"]),
                owner=self.owner,
                fencing_token=int(claim["fencing_token"]),
                sent=False,
                result_code=exc.code,
            )
            return
        self.store.finish_outbox(
            outbox_id=str(claim["outbox_id"]),
            owner=self.owner,
            fencing_token=int(claim["fencing_token"]),
            sent=True,
            result_code="ok",
        )

    async def scheduler_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.store.controller_heartbeat(owner=self.owner)
                reconciled = self.store.reconcile_next_job()
                if reconciled is not None:
                    continue
                outbox = self.store.claim_outbox(owner=self.owner, lease_seconds=self.lease_seconds)
                if outbox is not None:
                    await self._execute_outbox(outbox)
                    continue
                claim = self.store.claim_next_run(owner=self.owner, lease_seconds=self.lease_seconds)
                if claim is not None:
                    await self._execute_claim(claim)
                    continue
            except Exception:  # noqa: BLE001 - the persistent controller retries without logging private state.
                with suppress(Exception):
                    self.store.controller_heartbeat(owner=self.owner, state="error")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=self.tick_seconds)

    async def run(self) -> None:
        self.store.initialize()
        self.store.seed_defaults()
        await asyncio.to_thread(self.timer_controller.adopt_all_current)
        self.store.controller_heartbeat(owner=self.owner, started=True)
        await self.control_server.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stopping.set)
        scheduler = asyncio.create_task(self.scheduler_loop())
        notify_systemd("READY=1")
        try:
            await self.stopping.wait()
        finally:
            notify_systemd("STOPPING=1")
            scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler
            with suppress(Exception):
                self.store.controller_heartbeat(owner=self.owner, state="stopped")
            await self.control_server.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("daemon", nargs="?", default="daemon", choices=["daemon"])
    parser.parse_args(argv)
    load_runtime_env()
    asyncio.run(AutomationDaemon.production().run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
