"""Allowlisted, non-activating systemd timer inspection and drop-in staging."""

from __future__ import annotations

import os
import hashlib
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from .automation_registry import AutomationError, AutomationStore, isoformat, parse_time


SYSTEMD_TIMER_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,120}\.timer\Z")


@dataclass(frozen=True)
class SystemTimerPolicy:
    timer_id: str
    unit_name: str
    display_name: str
    control_mode: str = "read_only"

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.timer_id)
            or SYSTEMD_TIMER_PATTERN.fullmatch(self.unit_name) is None
            or self.control_mode not in {"read_only", "managed"}
        ):
            raise ValueError("system_timer_policy_invalid")


# Production timer control stays deny-by-default.  Adding a timer is a reviewed
# source change, not a value accepted from CRM, Telegram or an environment file.
SYSTEM_TIMER_ALLOWLIST: Mapping[str, SystemTimerPolicy] = MappingProxyType(
    {
        "managed_pc_health": SystemTimerPolicy(
            timer_id="managed_pc_health",
            unit_name="autostop-managed-pc-health.timer",
            display_name="Проверка управляемых ПК",
            control_mode="managed",
        ),
        "managed_pc_fleet_health": SystemTimerPolicy(
            timer_id="managed_pc_fleet_health",
            unit_name="autostop-managed-pc-fleet-health.timer",
            display_name="Сводная проверка парка ПК",
            control_mode="managed",
        ),
        "managed_pc_pending_cleanup": SystemTimerPolicy(
            timer_id="managed_pc_pending_cleanup",
            unit_name="autostop-managed-pc-pending-cleanup.timer",
            display_name="Очистка ожидающих подключений ПК",
            control_mode="managed",
        ),
        "database_backup": SystemTimerPolicy(
            timer_id="database_backup",
            unit_name="autostop24-db-backup.timer",
            display_name="Резервная копия CRM",
            control_mode="read_only",
        ),
        "app_watchdog": SystemTimerPolicy(
            timer_id="app_watchdog",
            unit_name="autostop-app-watchdog.timer",
            display_name="Watchdog приложения",
            control_mode="read_only",
        ),
    }
)


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _systemctl(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)


class SystemTimerController:
    def __init__(
        self,
        *,
        policies: Mapping[str, SystemTimerPolicy] = SYSTEM_TIMER_ALLOWLIST,
        runner: Runner = _systemctl,
        dropin_root: Path = Path("/etc/systemd/system"),
        store: AutomationStore | None = None,
    ) -> None:
        self.policies = dict(policies)
        self.runner = runner
        self.dropin_root = dropin_root
        self.store = store or AutomationStore()

    def _policy(self, timer_id: str) -> SystemTimerPolicy:
        policy = self.policies.get(timer_id)
        if policy is None:
            raise AutomationError("system_timer_not_allowed")
        return policy

    def inspect(self, timer_id: str) -> dict[str, Any]:
        policy = self._policy(timer_id)
        result = self.runner(
            [
                "systemctl",
                "show",
                policy.unit_name,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=UnitFileState",
                "--property=NextElapseUSecRealtime",
                "--property=NextElapseUSecMonotonic",
                "--property=LastTriggerUSec",
                "--property=Result",
                "--property=TimersCalendar",
                "--property=TimersMonotonic",
                "--no-pager",
            ]
        )
        properties: dict[str, list[str]] = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and len(value) <= 512:
                    properties.setdefault(key, []).append(value)
        one = lambda key, default="": (properties.get(key) or [default])[-1]  # noqa: E731
        monotonic = properties.get("TimersMonotonic", [])
        period_minutes: int | None = None
        for value in monotonic:
            match = re.search(r"OnUnitActiveUSec=(\d+)(s|min|h)\b", value)
            if match:
                amount = int(match.group(1))
                seconds = amount * {"s": 1, "min": 60, "h": 3600}[match.group(2)]
                if seconds % 60 == 0:
                    period_minutes = seconds // 60
                break
        last_run = one("LastTriggerUSec") or None
        next_run = one("NextElapseUSecRealtime") or None
        if next_run is None and period_minutes is not None:
            parsed_last = parse_time(last_run)
            if parsed_last is None and last_run:
                try:
                    parsed_last = datetime.strptime(last_run, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
                except ValueError:
                    parsed_last = None
            if parsed_last is not None:
                next_run = isoformat(parsed_last + timedelta(minutes=period_minutes))
        unit_file_state = one("UnitFileState", "unknown")
        active_state = one("ActiveState", "unknown")
        result_state = one("Result", "unknown")
        state = {
            "load_state": one("LoadState", "unknown"),
            "active_state": active_state,
            "unit_file_state": unit_file_state,
            "desired_state": "on" if unit_file_state in {"enabled", "enabled-runtime"} else "off",
            "actual_state": "active" if active_state == "active" else active_state,
            "period_minutes": period_minutes,
            "next_run_at": next_run,
            "last_run_at": last_run,
            "calendar": one("TimersCalendar") or None,
            "error_code": None if result_state in {"success", "unknown"} else "system_timer_failed",
        }
        dropin = self.dropin_root / f"{policy.unit_name}.d" / "50-autostop-automation.conf"
        try:
            dropin_info = dropin.lstat()
            if stat.S_ISREG(dropin_info.st_mode) and not stat.S_ISLNK(dropin_info.st_mode):
                content = dropin.read_bytes()
                if len(content) <= 4096 and content.startswith(b"# Generated by AutoStop Manager Automation Center.\n"):
                    state["dropin_sha256"] = hashlib.sha256(content).hexdigest()
        except OSError:
            pass
        return {
            "timer_id": policy.timer_id,
            "unit_name": policy.unit_name,
            "name": policy.display_name,
            "control_mode": policy.control_mode,
            "locked": policy.control_mode == "read_only",
            "inspection_ok": result.returncode == 0,
            "state": state,
        }

    def list_status(self) -> list[dict[str, Any]]:
        return [self.inspect(timer_id) for timer_id in sorted(self.policies)]

    def adopt_current(self, timer_id: str) -> dict[str, Any]:
        current = self.inspect(timer_id)
        if not current["inspection_ok"]:
            raise AutomationError("system_timer_inspection_failed")
        return self.store.adopt_system_timer(
            timer_id=timer_id,
            unit_name=str(current["unit_name"]),
            control_mode=str(current["control_mode"]),
            state=current["state"],
        )

    def adopt_all_current(self) -> list[dict[str, Any]]:
        adopted = []
        for timer_id in sorted(self.policies):
            try:
                adopted.append(self.adopt_current(timer_id))
            except AutomationError as exc:
                adopted.append({"timer_id": timer_id, "adopted": False, "error_code": exc.code})
        return adopted

    def render_interval_dropin(self, timer_id: str, *, every_minutes: int) -> str:
        policy = self._policy(timer_id)
        if policy.control_mode != "managed":
            raise AutomationError("system_timer_read_only")
        if type(every_minutes) is not int or not 5 <= every_minutes <= 24 * 60:
            raise AutomationError("system_timer_schedule_invalid")
        return (
            "# Generated by AutoStop Manager Automation Center.\n"
            "# Installation alone does not reload, enable, start or stop a unit.\n"
            "[Timer]\n"
            "OnCalendar=\n"
            "OnActiveSec=\n"
            "OnBootSec=\n"
            "OnStartupSec=\n"
            "OnUnitActiveSec=\n"
            "OnUnitInactiveSec=\n"
            f"OnUnitActiveSec={every_minutes}min\n"
            "AccuracySec=1min\n"
            "Persistent=true\n"
        )

    def _dropin_target(self, policy: SystemTimerPolicy) -> Path:
        return self.dropin_root / f"{policy.unit_name}.d" / "50-autostop-automation.conf"

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _capture_dropin(self, policy: SystemTimerPolicy) -> dict[str, Any]:
        target = self._dropin_target(policy)
        try:
            info = target.lstat()
        except FileNotFoundError:
            return {"present": False, "target": target}
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AutomationError("system_timer_dropin_target_invalid")
        content = target.read_bytes()
        if len(content) > 4096:
            raise AutomationError("system_timer_dropin_target_invalid")
        return {
            "present": True,
            "target": target,
            "content": content,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _restore_dropin(self, snapshot: Mapping[str, Any]) -> None:
        target = Path(str(snapshot["target"]))
        target_dir = target.parent
        info = target_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AutomationError("system_timer_rollback_failed")
        if not snapshot.get("present"):
            try:
                target_info = target.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
                    raise AutomationError("system_timer_rollback_failed")
                target.unlink()
            self._fsync_directory(target_dir)
            return
        content = snapshot.get("content")
        if not isinstance(content, bytes) or len(content) > 4096:
            raise AutomationError("system_timer_rollback_failed")
        temporary = target_dir / f".50-autostop-automation.rollback.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, int(snapshot["mode"]))
        try:
            os.write(descriptor, content)
            os.fchmod(descriptor, int(snapshot["mode"]))
            os.fchown(descriptor, int(snapshot["uid"]), int(snapshot["gid"]))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, target)
            self._fsync_directory(target_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def stage_interval_dropin(
        self,
        timer_id: str,
        *,
        every_minutes: int,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(timer_id)
        content = self.render_interval_dropin(timer_id, every_minutes=every_minutes)
        root = self.dropin_root
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise AutomationError("system_timer_dropin_root_invalid")
        target_dir = root / f"{policy.unit_name}.d"
        try:
            info = target_dir.lstat()
        except FileNotFoundError:
            target_dir.mkdir(mode=0o755)
            info = target_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AutomationError("system_timer_dropin_directory_invalid")
        if not target_dir.resolve().is_relative_to(root.resolve()):
            raise AutomationError("system_timer_dropin_directory_invalid")
        target = self._dropin_target(policy)
        snapshot = self._capture_dropin(policy)
        previous_sha256 = snapshot.get("sha256")
        if snapshot["present"]:
            previous = snapshot["content"]
            if previous == content.encode():
                return {
                    "timer_id": timer_id,
                    "path": str(target),
                    "staged": True,
                    "changed": False,
                    "sha256": previous_sha256,
                    "daemon_reload_required": False,
                    "unit_state_changed": False,
                }
            if expected_sha256 is None or expected_sha256 != previous_sha256:
                raise AutomationError("system_timer_dropin_revision_conflict")
        temporary = target_dir / f".50-autostop-automation.{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o644)
        try:
            os.write(descriptor, content.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, target)
            os.chmod(target, 0o644, follow_symlinks=False)
            with target.open("rb") as installed:
                os.fsync(installed.fileno())
            self._fsync_directory(target_dir)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "timer_id": timer_id,
            "path": str(target),
            "staged": True,
            "changed": True,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "previous_sha256": previous_sha256,
            "daemon_reload_required": True,
            "unit_state_changed": False,
        }

    def _systemctl_action(self, *arguments: str) -> None:
        result = self.runner(["systemctl", *arguments, "--no-pager"])
        if result.returncode != 0:
            raise AutomationError("system_timer_apply_failed")

    def set_enabled(self, timer_id: str, *, enabled: bool) -> dict[str, Any]:
        policy = self._policy(timer_id)
        if policy.control_mode != "managed":
            raise AutomationError("system_timer_read_only")
        if type(enabled) is not bool:
            raise AutomationError("system_timer_enabled_invalid")
        before = self.inspect(timer_id)
        if not before["inspection_ok"]:
            raise AutomationError("system_timer_inspection_failed")
        expected_desired = "on" if enabled else "off"
        expected_actual = "active" if enabled else "inactive"
        original_error: AutomationError | None = None
        try:
            self._systemctl_action("enable" if enabled else "disable", "--now", policy.unit_name)
            status = self.inspect(timer_id)
            if (
                not status["inspection_ok"]
                or status["state"]["desired_state"] != expected_desired
                or status["state"]["actual_state"] != expected_actual
            ):
                raise AutomationError("system_timer_readback_failed")
            return status
        except AutomationError as exc:
            original_error = exc
        try:
            previous_desired = str(before["state"]["desired_state"])
            previous_actual = str(before["state"]["actual_state"])
            self._systemctl_action("enable" if previous_desired == "on" else "disable", policy.unit_name)
            self._systemctl_action("start" if previous_actual == "active" else "stop", policy.unit_name)
            restored = self.inspect(timer_id)
            if (
                not restored["inspection_ok"]
                or restored["state"]["desired_state"] != previous_desired
                or restored["state"]["actual_state"] != previous_actual
            ):
                raise AutomationError("system_timer_rollback_failed")
        except AutomationError as exc:
            raise AutomationError("system_timer_rollback_failed") from exc
        if original_error is None:
            raise AutomationError("system_timer_apply_failed")
        raise original_error

    def set_schedule(
        self,
        timer_id: str,
        *,
        every_minutes: int,
        expected_dropin_sha256: str | None = None,
    ) -> dict[str, Any]:
        policy = self._policy(timer_id)
        if policy.control_mode != "managed":
            raise AutomationError("system_timer_read_only")
        before = self.inspect(timer_id)
        if not before["inspection_ok"]:
            raise AutomationError("system_timer_inspection_failed")
        snapshot = self._capture_dropin(policy)
        original_error: AutomationError | None = None
        try:
            staged = self.stage_interval_dropin(
                timer_id,
                every_minutes=every_minutes,
                expected_sha256=expected_dropin_sha256,
            )
            self._systemctl_action("daemon-reload")
            if before["state"]["actual_state"] == "active":
                self._systemctl_action("restart", policy.unit_name)
            status = self.inspect(timer_id)
            if not status["inspection_ok"] or status["state"]["period_minutes"] != every_minutes:
                raise AutomationError("system_timer_readback_failed")
            return {**status, "dropin_sha256": staged["sha256"], "dropin_changed": staged["changed"]}
        except (AutomationError, OSError) as exc:
            if isinstance(exc, AutomationError) and exc.code in {
                "system_timer_dropin_revision_conflict",
                "system_timer_schedule_invalid",
                "system_timer_dropin_root_invalid",
                "system_timer_dropin_directory_invalid",
                "system_timer_dropin_target_invalid",
            }:
                raise
            original_error = exc
        try:
            self._restore_dropin(snapshot)
            self._systemctl_action("daemon-reload")
            if before["state"]["actual_state"] == "active":
                self._systemctl_action("restart", policy.unit_name)
            restored = self.inspect(timer_id)
            restored_snapshot = self._capture_dropin(policy)
            if (
                not restored["inspection_ok"]
                or restored["state"]["period_minutes"] != before["state"]["period_minutes"]
                or restored["state"]["desired_state"] != before["state"]["desired_state"]
                or restored["state"]["actual_state"] != before["state"]["actual_state"]
                or bool(restored_snapshot["present"]) != bool(snapshot["present"])
                or restored_snapshot.get("sha256") != snapshot.get("sha256")
            ):
                raise AutomationError("system_timer_rollback_failed")
        except (AutomationError, OSError) as exc:
            raise AutomationError("system_timer_rollback_failed") from exc
        if isinstance(original_error, AutomationError):
            raise original_error
        raise AutomationError("system_timer_apply_failed") from original_error
