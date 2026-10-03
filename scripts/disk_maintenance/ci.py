"""Bounded CI admission/drain and durable recovery; never cancel or dispatch jobs."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .util import MaintenanceError, atomic_json, read_json, run

REPOSITORY = "AutoStopKrsk/AutoStop-App"
RUNNER_UNIT = "actions.runner.AutoStopKrsk-AutoStop-App.vps26457.service"
RUNNER_ROOT = "/opt/actions-runner"
BUSY_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
WORKFLOW_STATES = {"active", "deleted", "disabled_fork", "disabled_inactivity", "disabled_manually"}
PHASES = {"pausing", "paused", "restoring", "restored", "recovery_required"}


def _settings(policy: dict) -> dict:
    value = policy.get("ci", {})
    if not isinstance(value, dict):
        raise MaintenanceError("ci_policy_invalid")
    settings = {
        "repository": value.get("repository", REPOSITORY),
        "runner_unit": value.get("runner_unit", RUNNER_UNIT),
        "runner_root": value.get("runner_root", RUNNER_ROOT),
        "poll_seconds": value.get("poll_seconds", 5),
    }
    if any(
        settings[key] != expected
        for key, expected in (("repository", REPOSITORY), ("runner_unit", RUNNER_UNIT), ("runner_root", RUNNER_ROOT))
    ):
        raise MaintenanceError("ci_target_invalid")
    poll = settings["poll_seconds"]
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0.1 <= poll <= 30:
        raise MaintenanceError("ci_policy_invalid")
    return settings


def _command(argv: list[str], *, timeout: float = 30) -> str:
    try:
        result = run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MaintenanceError("ci_command_timeout") from None
    except OSError:
        raise MaintenanceError("ci_command_failed") from None
    if result.returncode:
        code = "ci_api_auth_failed" if argv[0] == "gh" and result.returncode == 4 else "ci_command_failed"
        raise MaintenanceError(code)
    return result.stdout


def _api(settings: dict, endpoint: str, *, method: str = "GET") -> dict:
    argv = [
        "gh",
        "api",
        "--method",
        method,
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{settings['repository']}/actions/{endpoint}",
    ]
    timeout = 30.0
    if settings.get("_drain_deadline") is not None:
        remaining = settings["_drain_deadline"] - time.monotonic()
        if remaining <= 0:
            raise MaintenanceError("ci_drain_timeout")
        timeout = min(timeout, remaining)
    output = _command(argv, timeout=timeout)
    if method == "PUT":
        return {}
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        raise MaintenanceError("ci_api_response_invalid") from None
    if not isinstance(value, dict):
        raise MaintenanceError("ci_api_response_invalid")
    return value


def _records(settings: dict, endpoint: str, key: str) -> list[dict]:
    records: list[dict] = []
    identifiers: set[int] = set()
    expected_total = None
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 1001):
        value = _api(settings, f"{endpoint}{separator}per_page=100&page={page}")
        total, entries = value.get("total_count"), value.get(key)
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or not isinstance(entries, list)
            or len(entries) > 100
        ):
            raise MaintenanceError("ci_api_response_invalid")
        if expected_total is not None and total != expected_total:
            raise MaintenanceError("ci_catalog_changed")
        expected_total = total
        for entry in entries:
            if not isinstance(entry, dict) or not _identifier(entry.get("id")):
                raise MaintenanceError("ci_api_response_invalid")
            if entry["id"] in identifiers:
                raise MaintenanceError("ci_catalog_changed")
            identifiers.add(entry["id"])
            records.append(entry)
        if len(records) == total:
            return records
        if len(records) > total or not entries:
            raise MaintenanceError("ci_catalog_changed")
    raise MaintenanceError("ci_pagination_limit")


def _identifier(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _workflow(value: dict) -> dict:
    path, status, updated = value.get("path"), value.get("state"), value.get("updated_at")
    if (
        not _identifier(value.get("id"))
        or not isinstance(status, str)
        or status not in WORKFLOW_STATES
        or not isinstance(path, str)
        or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path)
        or not _timestamp(updated)
    ):
        raise MaintenanceError("ci_workflow_invalid")
    return {"id": value["id"], "path": path, "state": status, "updated_at": updated}


def _timestamp(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value))


def _repository_head(settings: dict) -> str:
    head = _command(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "--jq",
            ".sha",
            f"repos/{settings['repository']}/commits/main",
        ]
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise MaintenanceError("ci_repository_head_invalid")
    return head


def _workflows(settings: dict) -> list[dict]:
    return [_workflow(value) for value in _records(settings, "workflows", "workflows")]


def _get_workflow(settings: dict, saved: dict) -> dict:
    current = _workflow(_api(settings, f"workflows/{saved['id']}"))
    if current["id"] != saved["id"] or current["path"] != saved["path"]:
        raise MaintenanceError("ci_workflow_identity_changed")
    return current


def _runs_busy(settings: dict) -> bool:
    for status in BUSY_STATUSES:
        values = _records(settings, f"runs?status={status}", "workflow_runs")
        if any(value.get("status") != status for value in values):
            raise MaintenanceError("ci_run_status_invalid")
        if values:
            return True
    return False


def _process_fences() -> tuple[bool, bool]:
    """Read technical process bindings only; never persist argv or environment."""
    worker = deploy = False
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        raise MaintenanceError("ci_process_probe_failed") from None
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            comm = (process / "comm").read_text().strip()
            worker = worker or comm in {"Runner.Worker", "Runner.Worker.e"}
            if comm not in {"bash", "sh", "dash", "ssh", "pwsh", "powershell"}:
                continue
            command = (process / "cmdline").read_bytes()
            deploy = deploy or any(
                marker in command
                for marker in (
                    b"store_deploy_",
                    b"STORE_DEPLOY_ROOT",
                    b"store_deploy_remote.sh",
                    b"deploy-production-ssh.ps1",
                    b"deploy-production.ps1",
                )
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            raise MaintenanceError("ci_process_probe_failed") from None
    return worker, deploy


def _runner(settings: dict) -> dict:
    output = _command(
        [
            "systemctl",
            "show",
            settings["runner_unit"],
            "--property=Id,ActiveState,SubState,MainPID,InvocationID",
        ]
    )
    value = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if value.get("Id") != settings["runner_unit"]:
        raise MaintenanceError("ci_runner_identity_invalid")
    try:
        pid = int(value.get("MainPID", ""))
    except ValueError:
        raise MaintenanceError("ci_runner_identity_invalid") from None
    invocation = value.get("InvocationID", "")
    if pid < 0 or (invocation and not re.fullmatch(r"[0-9a-f]{32}", invocation)):
        raise MaintenanceError("ci_runner_identity_invalid")
    return {
        "active_state": value.get("ActiveState"),
        "sub_state": value.get("SubState"),
        "main_pid": pid,
        "invocation_id": invocation,
    }


def _save(path: Path, state: dict) -> None:
    try:
        atomic_json(path, state)
    except OSError:
        raise MaintenanceError("ci_state_write_failed") from None


def _load(settings: dict, path: Path) -> dict:
    state = read_json(path, private=True)
    if (
        not isinstance(state, dict)
        or state.get("schema") != 1
        or state.get("repository") != settings["repository"]
        or state.get("runner_unit") != settings["runner_unit"]
        or not isinstance(state.get("phase"), str)
        or state["phase"] not in PHASES
        or not isinstance(state.get("operation_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", state["operation_id"])
        or not isinstance(state.get("workflows"), list)
        or not isinstance(state.get("runner_original"), dict)
    ):
        raise MaintenanceError("ci_state_invalid")
    original = state["runner_original"]
    for key in ("repository_head_before", "repository_head_after"):
        value = state.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)):
            raise MaintenanceError("ci_state_invalid")
    if not isinstance(state.get("admission_gap_possible", False), bool):
        raise MaintenanceError("ci_state_invalid")
    warnings = state.get("warnings", [])
    if not isinstance(warnings, list) or any(
        value
        not in (
            "ci_repository_head_before_unavailable",
            "ci_repository_head_changed",
            "ci_repository_head_readback_unavailable",
        )
        for value in warnings
    ):
        raise MaintenanceError("ci_state_invalid")
    if not _valid_runner_original(original):
        raise MaintenanceError("ci_state_invalid")
    if any(
        not isinstance(state.get(key), bool)
        for key in ("runner_stop_intent", "runner_stopped", "runner_restored", "runner_start_intent")
    ):
        raise MaintenanceError("ci_state_invalid")
    if (state["runner_stop_intent"] and original["active_state"] != "active") or (
        (state["runner_stopped"] or state["runner_start_intent"]) and not state["runner_stop_intent"]
    ):
        raise MaintenanceError("ci_state_invalid")
    seen: set[int] = set()
    for entry in state["workflows"]:
        if not isinstance(entry, dict):
            raise MaintenanceError("ci_state_invalid")
        _workflow(
            {
                "id": entry.get("id"),
                "path": entry.get("path"),
                "state": entry.get("original_state"),
                "updated_at": entry.get("original_updated_at"),
            }
        )
        if entry["id"] in seen or any(
            not isinstance(entry.get(key), bool) for key in ("disable_intent", "disabled", "restored")
        ):
            raise MaintenanceError("ci_state_invalid")
        if entry.get("disabled_updated_at") is not None and not _timestamp(entry["disabled_updated_at"]):
            raise MaintenanceError("ci_state_invalid")
        if entry["disabled"] and (not entry["disable_intent"] or entry["disabled_updated_at"] is None):
            raise MaintenanceError("ci_state_invalid")
        if entry["original_state"] != "active" and (entry["disable_intent"] or entry["disabled"]):
            raise MaintenanceError("ci_state_invalid")
        seen.add(entry["id"])
    if state["phase"] == "restored" and (
        not state["runner_restored"] or not all(entry["restored"] for entry in state["workflows"])
    ):
        raise MaintenanceError("ci_state_invalid")
    return state


def _valid_runner_original(value: dict) -> bool:
    pair = (value.get("active_state"), value.get("sub_state"))
    pid, invocation = value.get("main_pid"), value.get("invocation_id")
    if (
        not all(isinstance(item, str) for item in pair)
        or pair not in {("active", "running"), ("inactive", "dead")}
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 0
        or not isinstance(invocation, str)
        or (invocation and not re.fullmatch(r"[0-9a-f]{32}", invocation))
    ):
        return False
    return (pid > 0 and bool(invocation)) if pair[0] == "active" else pid == 0


def _admission_closed(settings: dict, state: dict) -> None:
    current = {entry["id"]: entry for entry in _workflows(settings)}
    if set(current) != {entry["id"] for entry in state["workflows"]}:
        raise MaintenanceError("ci_catalog_changed")
    for saved in state["workflows"]:
        value = current[saved["id"]]
        expected = "disabled_manually" if saved["original_state"] == "active" else saved["original_state"]
        if value["path"] != saved["path"] or value["state"] != expected:
            raise MaintenanceError("ci_admission_changed")


def _disable(settings: dict, path: Path, state: dict) -> None:
    for saved in state["workflows"]:
        if saved["original_state"] != "active":
            continue
        current = _get_workflow(settings, saved)
        if current["state"] != "active" or current["updated_at"] != saved["original_updated_at"]:
            raise MaintenanceError("ci_workflow_state_changed")
        saved["disable_intent"] = True
        state["admission_gap_possible"] = True
        _save(path, state)
        _api(settings, f"workflows/{saved['id']}/disable", method="PUT")
        current = _get_workflow(settings, saved)
        if current["state"] != "disabled_manually":
            raise MaintenanceError("ci_disable_unconfirmed")
        saved["disabled"] = True
        saved["disabled_updated_at"] = current["updated_at"]
        _save(path, state)
    _admission_closed(settings, state)


def _drain(settings: dict, state: dict, wait_seconds: float) -> None:
    deadline = time.monotonic() + wait_seconds
    bounded_settings = {**settings, "_drain_deadline": deadline} if wait_seconds else settings
    while True:
        _admission_closed(bounded_settings, state)
        worker, deploy = _process_fences()
        if not _runs_busy(bounded_settings) and not worker and not deploy:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MaintenanceError("ci_drain_timeout")
        time.sleep(min(settings["poll_seconds"], remaining))


def _stop_runner(settings: dict, path: Path, state: dict) -> None:
    original = state["runner_original"]
    current = _runner(settings)
    if current != original:
        raise MaintenanceError("ci_runner_changed")
    if original["active_state"] == "inactive":
        return
    _admission_closed(settings, state)
    if _runs_busy(settings) or any(_process_fences()):
        raise MaintenanceError("ci_drain_race")
    state["runner_stop_intent"] = True
    _save(path, state)
    # The durable write can take time: repeat the local job fence after fsync.
    if any(_process_fences()):
        raise MaintenanceError("ci_drain_race")
    _command(["systemctl", "stop", settings["runner_unit"]], timeout=45)
    current = _runner(settings)
    if current["active_state"] != "inactive" or current["main_pid"] or any(_process_fences()):
        raise MaintenanceError("ci_runner_stop_unconfirmed")
    state["runner_stopped"] = True
    _save(path, state)


def active(policy: dict) -> bool:
    """Conservative queue/worker/deploy test; API/probe errors fail closed."""
    settings = _settings(policy)
    return any(_process_fences()) or _runs_busy(settings)


def pause(policy: dict, state_path: str | Path, *, wait_seconds: float = 1800) -> dict:
    settings, path = _settings(policy), Path(state_path)
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 1800:
        raise MaintenanceError("ci_wait_invalid")
    if path.exists() or path.is_symlink():
        old = _load(settings, path)
        if old["phase"] != "restored":
            resume(policy, path)
    runner = _runner(settings)
    if not _valid_runner_original(runner):
        raise MaintenanceError("ci_runner_not_stable")
    state = {
        "schema": 1,
        "operation_id": uuid.uuid4().hex,
        "repository": settings["repository"],
        "runner_unit": settings["runner_unit"],
        "created_at": datetime.now(UTC).isoformat(),
        "phase": "pausing",
        "runner_original": runner,
        "runner_stop_intent": False,
        "runner_stopped": False,
        "runner_start_intent": False,
        "runner_restored": False,
        "workflows": [
            {
                "id": entry["id"],
                "path": entry["path"],
                "original_state": entry["state"],
                "original_updated_at": entry["updated_at"],
                "disable_intent": False,
                "disabled": False,
                "disabled_updated_at": None,
                "restored": False,
            }
            for entry in _workflows(settings)
        ],
        "resume_errors": [],
        "repository_head_before": _repository_head(settings),
        "repository_head_after": None,
        "admission_gap_possible": False,
        "warnings": [],
    }
    _save(path, state)
    try:
        _disable(settings, path, state)
        _drain(settings, state, wait_seconds)
        _stop_runner(settings, path, state)
        state["phase"] = "paused"
        _save(path, state)
    except (MaintenanceError, OSError, ValueError, TypeError, KeyboardInterrupt) as exc:
        try:
            resume(policy, path)
        except MaintenanceError:
            raise MaintenanceError("ci_pause_recovery_required") from None
        if isinstance(exc, (MaintenanceError, KeyboardInterrupt)):
            raise
        raise MaintenanceError("ci_pause_failed") from None
    return state


def _restore_runner(settings: dict, path: Path, state: dict) -> None:
    if state["runner_restored"]:
        return
    if state["runner_stop_intent"] and state["runner_original"]["active_state"] == "active":
        current = _runner(settings)
        if current["active_state"] not in {"active", "inactive"}:
            raise MaintenanceError("ci_runner_recovery_not_stable")
        if current["active_state"] == "inactive":
            if any(_process_fences()):
                raise MaintenanceError("ci_runner_recovery_busy")
            state["runner_start_intent"] = True
            _save(path, state)
            _command(["systemctl", "start", settings["runner_unit"]], timeout=45)
            current = _runner(settings)
        if (current["active_state"], current["sub_state"]) != ("active", "running"):
            raise MaintenanceError("ci_runner_start_unconfirmed")
    state["runner_restored"] = True
    _save(path, state)


def _restore_workflow(settings: dict, path: Path, state: dict, saved: dict) -> None:
    if saved["restored"]:
        return
    if saved["original_state"] == "active" and saved["disable_intent"]:
        current = _get_workflow(settings, saved)
        if current["state"] == "disabled_manually":
            observed = saved["disabled_updated_at"]
            if observed is not None and current["updated_at"] != observed:
                raise MaintenanceError("ci_workflow_recovery_conflict")
            _api(settings, f"workflows/{saved['id']}/enable", method="PUT")
            if _get_workflow(settings, saved)["state"] != "active":
                raise MaintenanceError("ci_enable_unconfirmed")
        elif current["state"] != "active":
            raise MaintenanceError("ci_workflow_recovery_conflict")
    saved["restored"] = True
    _save(path, state)


def resume(policy: dict, state_path: str | Path) -> dict:
    """Recover only recorded own transitions; safe to repeat after interruption."""
    settings, path = _settings(policy), Path(state_path)
    if not path.exists() and not path.is_symlink():
        return {"restored": True, "phase": "absent", "workflow_changes": 0}
    state = _load(settings, path)
    if state["phase"] == "restored":
        return _resume_receipt(state, 0)
    state["phase"], state["resume_errors"] = "restoring", []
    _save(path, state)
    try:
        _restore_runner(settings, path, state)
    except MaintenanceError as exc:
        state["resume_errors"].append(str(exc))
    changes = 0
    for saved in state["workflows"]:
        was_restored = saved["restored"]
        try:
            _restore_workflow(settings, path, state, saved)
            changes += int(not was_restored and saved["disable_intent"])
        except MaintenanceError as exc:
            state["resume_errors"].append(str(exc))
    state["phase"] = "recovery_required" if state["resume_errors"] else "restored"
    if not state["resume_errors"]:
        _head_readback(settings, state)
    _save(path, state)
    if state["resume_errors"]:
        raise MaintenanceError("ci_resume_incomplete")
    return _resume_receipt(state, changes)


def _head_readback(settings: dict, state: dict) -> None:
    state.setdefault("warnings", [])
    state.setdefault("admission_gap_possible", any(entry["disable_intent"] for entry in state["workflows"]))
    try:
        state["repository_head_after"] = _repository_head(settings)
        before = state.get("repository_head_before")
        if before is None:
            state["warnings"].append("ci_repository_head_before_unavailable")
        elif before != state["repository_head_after"]:
            state["warnings"].append("ci_repository_head_changed")
    except MaintenanceError:
        state["repository_head_after"] = None
        state["warnings"].append("ci_repository_head_readback_unavailable")


def _resume_receipt(state: dict, changes: int) -> dict:
    return {
        "restored": True,
        "phase": "restored",
        "workflow_changes": changes,
        "repository_head_before": state.get("repository_head_before"),
        "repository_head_after": state.get("repository_head_after"),
        "admission_gap_possible": state.get("admission_gap_possible", False),
        "warnings": state.get("warnings", []),
    }
