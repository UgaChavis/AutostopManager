from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRIOR_UNIT = b"synthetic prior unit\n"
PRIOR_CONFIG = b"synthetic prior config\n"
CANDIDATE_UNIT = b"synthetic candidate unit\n"
CANDIDATE_CONFIG = b"synthetic candidate config\n"


@dataclass
class Outcome:
    process: subprocess.CompletedProcess[str]
    root: Path
    backup: Path
    unit: Path
    config: Path
    calls: list[str]
    file_calls: list[str]
    service_state: tuple[int, int]


def finish_install_case(
    tmp_path: Path,
    *,
    file_flags: tuple[int, int] = (1, 1),
    service_flags: tuple[int, int] = (1, 1),
    fail_copy: str = "",
    fail_remove: str = "",
    fail_service: str = "",
    exit_code: int = 23,
    armed: int = 1,
    trigger: str = "exit",
) -> Outcome:
    source = (ROOT / "scripts" / "install-manager-automation.sh").read_text()
    body = source.split("finish_install() {\n", 1)[1].split("\n}\ntrap finish_install EXIT", 1)[0]
    activation_start = source.index('if systemctl is-active --quiet "${UNIT_NAME}"; then\n  systemctl enable')
    activation = "activation_started=1\n" + source[activation_start : source.index("\nfi", activation_start) + 3]
    backup = tmp_path / "backup"
    backup.mkdir()
    unit, config = tmp_path / "unit", tmp_path / "config"
    unit.write_bytes(CANDIDATE_UNIT)
    config.write_bytes(CANDIDATE_CONFIG)
    if file_flags[0]:
        (backup / "unit").write_bytes(PRIOR_UNIT)
    if file_flags[1]:
        (backup / "config").write_bytes(PRIOR_CONFIG)
    temporary = tmp_path / "temporary-config"
    temporary.write_bytes(b"synthetic uncommitted temporary config\n")
    calls, file_calls = tmp_path / "service-calls", tmp_path / "file-calls"
    state = tmp_path / "service-state"
    variables = {
        "private_root": str(tmp_path),
        "rollback_dir": str(backup),
        "UNIT_PATH": str(unit),
        "CONFIG_PATH": str(config),
        "temporary_config": str(temporary),
        "calls": str(calls),
        "file_calls": str(file_calls),
        "state": str(state),
        "active": str(service_flags[0]),
        "enabled": str(service_flags[1]),
        "rollback_phase": "0" if trigger == "candidate_health" else "1",
        "FAIL_COPY": fail_copy,
        "FAIL_REMOVE": fail_remove,
        "FAIL_SERVICE": fail_service,
        "unit_existed": str(file_flags[0]),
        "config_existed": str(file_flags[1]),
        "service_was_active": str(service_flags[0]),
        "service_was_enabled": str(service_flags[1]),
        "rollback_armed": str(armed),
        "activation_started": "0",
        "UNIT_NAME": "synthetic-manager-review.service",
        "installation_failure": trigger if trigger != "exit" else "",
        "original_failure": str(exit_code),
    }
    script = "set -Eeuo pipefail\n"
    script += "".join(name + "=" + shlex.quote(value) + "\n" for name, value in variables.items())
    script += r"""
private_paths() {
  local item
  for item in "$@"; do
    [[ "$item" == -* ]] && continue
    [[ "$item" == "$private_root"/* ]] || { printf 'forbidden_path\n' >&2; exit 90; }
  done
}
cp() {
  private_paths "$@"
  local destination="${@: -1}"
  if [[ "$installation_failure" == candidate_copy ]]; then
    installation_failure=''
    return "$original_failure"
  fi
  printf 'copy:%s\n' "${destination##*/}" >> "$file_calls"
  if [[ ",${FAIL_COPY}," == *",${destination##*/},"* ]]; then
    printf 'synthetic partial copy\n' > "$destination"
    return 7
  fi
  command /bin/cp "$@"
}
rm() {
  private_paths "$@"
  local destination="${@: -1}"
  printf 'remove:%s\n' "${destination##*/}" >> "$file_calls"
  if [[ -n "$FAIL_REMOVE" && "$destination" == "$private_root/$FAIL_REMOVE" ]]; then return 7; fi
  command /bin/rm "$@"
}
systemctl() {
  if [[ "$installation_failure" == candidate_activation ]]; then
    [[ "$*" == "enable --now $UNIT_NAME" ]] || exit 90
    installation_failure=''
    active=1
    enabled=1
    persist_state
    return "$original_failure"
  fi
  local command="$1"
  case "$command" in
    is-active)
      [[ "$*" == "is-active --quiet $UNIT_NAME" ]] || exit 90
      [[ "$active" == 1 ]]
      return ;;
    enable)
      if [[ "$*" == "enable --now $UNIT_NAME" ]]; then
        [[ "$rollback_phase" == 0 ]] || exit 90
        active=1
        enabled=1
        persist_state
        return 0
      fi
      [[ $# -eq 2 && "$2" == "$UNIT_NAME" ]] || exit 90 ;;
    daemon-reload) [[ $# -eq 1 ]] || exit 90 ;;
    disable|restart|stop) [[ $# -eq 2 && "$2" == "$UNIT_NAME" ]] || exit 90 ;;
    *) exit 90 ;;
  esac
  if [[ "$rollback_phase" == 1 ]]; then
    printf '%s\n' "$command" >> "$calls"
    if [[ "$command" == "$FAIL_SERVICE" ]]; then return 7; fi
  fi
  case "$command" in
    enable) enabled=1 ;;
    disable) enabled=0 ;;
    restart) active=1 ;;
    stop) active=0 ;;
  esac
  persist_state
}
persist_state() {
  printf '%s %s\n' "$active" "$enabled" > "$state"
}
"""
    script += "finish_install() {\n" + body + "\n}\ntrap finish_install EXIT\npersist_state\n"
    if trigger == "candidate_copy":
        script += 'cp --archive --no-dereference "$rollback_dir/unit" "$UNIT_PATH"\n'
    elif trigger == "candidate_activation":
        script += 'activation_started=1\nsystemctl enable --now "$UNIT_NAME"\n'
    elif trigger == "candidate_health":
        script += activation + f"\nrollback_phase=1\nexit {exit_code}\n"
    else:
        script += f"exit {exit_code}\n"
    environment = {
        "PATH": str(tmp_path / "empty-executor-path"),
        "LANG": "C.UTF-8",
        "AUTOSTOP_MANAGER_ENV_FILE": "/dev/null",
        "AUTOSTOP_MANAGER_DB": str(tmp_path / "db.sqlite3"),
        "AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME": str(tmp_path / "offline-unavailable"),
        "AUTOSTOP_J1_CACHE_DIR": str(tmp_path / "j1"),
        "AUTOSTOP_J1_VIN_CACHE_DIR": str(tmp_path / "vin"),
        "AUTOSTOP_J1_VIN_RESEARCH_ENABLED": "0",
    }
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == exit_code, result.stderr
    assert "forbidden_path" not in result.stderr
    assert not temporary.exists()
    return Outcome(
        result,
        tmp_path,
        backup,
        unit,
        config,
        calls.read_text().splitlines() if calls.exists() else [],
        file_calls.read_text().splitlines() if file_calls.exists() else [],
        tuple(map(int, state.read_text().split())),
    )


def require_backup_retained(outcome: Outcome, file_flags: tuple[int, int] = (1, 1)) -> None:
    assert outcome.backup.exists(), "incomplete restore must retain the sole prior copies"
    if file_flags[0]:
        assert (outcome.backup / "unit").read_bytes() == PRIOR_UNIT
    if file_flags[1]:
        assert (outcome.backup / "config").read_bytes() == PRIOR_CONFIG
    assert "manager_automation_installer_rollback_attempted=true" in outcome.process.stderr
    assert "manager_automation_installer_rollback_incomplete=true" in outcome.process.stderr
    assert str(outcome.backup) in outcome.process.stderr


@pytest.mark.parametrize("active,enabled", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_complete_restore_restores_prior_files_and_service_lifecycle_and_cleans_backup(tmp_path, active, enabled):
    result = finish_install_case(tmp_path, service_flags=(active, enabled))
    assert result.unit.read_bytes() == PRIOR_UNIT
    assert result.config.read_bytes() == PRIOR_CONFIG
    assert result.calls == ["daemon-reload", "enable" if enabled else "disable", "restart" if active else "stop"]
    assert not result.backup.exists()
    assert "manager_automation_installer_rollback_attempted=true" in result.process.stderr
    assert "rollback_incomplete" not in result.process.stderr


@pytest.mark.parametrize("file_flags", [(0, 0), (0, 1), (1, 0)])
def test_restore_removes_only_files_that_did_not_exist_before_install(tmp_path, file_flags):
    result = finish_install_case(tmp_path, file_flags=file_flags, service_flags=(0, 0))
    assert result.unit.exists() == bool(file_flags[0])
    assert result.config.exists() == bool(file_flags[1])
    if file_flags[0]:
        assert result.unit.read_bytes() == PRIOR_UNIT
    if file_flags[1]:
        assert result.config.read_bytes() == PRIOR_CONFIG
    assert result.calls == ["daemon-reload", "disable", "stop"]
    assert not result.backup.exists()


@pytest.mark.parametrize("service_flags", [(0, 0), (1, 1)])
@pytest.mark.parametrize(
    "operation,target", [("copy", "unit"), ("copy", "config"), ("remove", "unit"), ("remove", "config")]
)
def test_incomplete_file_restore_retains_backup_and_never_activates_partial_file_set(
    tmp_path, service_flags, operation, target
):
    file_flags = (int(target != "unit"), int(target != "config")) if operation == "remove" else (1, 1)
    kwargs = {"fail_copy" if operation == "copy" else "fail_remove": target}
    result = finish_install_case(tmp_path, file_flags=file_flags, service_flags=service_flags, **kwargs)
    require_backup_retained(result, file_flags)
    assert result.calls == (["stop"] if not service_flags[0] else [])
    other_path, prior = (result.config, PRIOR_CONFIG) if target == "unit" else (result.unit, PRIOR_UNIT)
    assert other_path.read_bytes() == prior


def test_both_file_restorations_are_attempted_independently_before_service_steps(tmp_path):
    result = finish_install_case(tmp_path, fail_copy="unit,config")
    require_backup_retained(result)
    assert result.file_calls.count("copy:unit") == result.file_calls.count("copy:config") == 1
    assert result.calls == []


@pytest.mark.parametrize("service_flags", [(0, 0), (1, 1)])
def test_failed_daemon_reload_retains_restored_files_and_backup_without_activation(tmp_path, service_flags):
    result = finish_install_case(tmp_path, service_flags=service_flags, fail_service="daemon-reload")
    require_backup_retained(result)
    assert result.unit.read_bytes() == PRIOR_UNIT and result.config.read_bytes() == PRIOR_CONFIG
    assert result.calls == ["daemon-reload"] + (["stop"] if not service_flags[0] else [])


@pytest.mark.parametrize(
    "command,service_flags,expected",
    [
        ("enable", (1, 1), ["daemon-reload", "enable"]),
        ("disable", (0, 0), ["daemon-reload", "disable", "stop"]),
        ("restart", (1, 1), ["daemon-reload", "enable", "restart"]),
        ("stop", (0, 0), ["daemon-reload", "disable", "stop"]),
    ],
)
def test_unconfirmed_service_restore_retains_backup_and_stops_after_failed_phase(
    tmp_path, command, service_flags, expected
):
    result = finish_install_case(tmp_path, service_flags=service_flags, fail_service=command)
    require_backup_retained(result)
    assert result.unit.read_bytes() == PRIOR_UNIT and result.config.read_bytes() == PRIOR_CONFIG
    assert result.calls == expected


@pytest.mark.parametrize("exit_code,armed", [(0, 1), (0, 0), (23, 0)])
def test_success_or_disarmed_exit_preserves_candidate_files_without_service_rollback(tmp_path, exit_code, armed):
    result = finish_install_case(tmp_path, exit_code=exit_code, armed=armed)
    assert result.unit.read_bytes() == CANDIDATE_UNIT
    assert result.config.read_bytes() == CANDIDATE_CONFIG
    assert result.calls == [] and not result.backup.exists()
    assert "rollback_attempted" not in result.process.stderr


@pytest.mark.parametrize("trigger", ["exit", "candidate_copy"])
def test_original_installer_failure_status_survives_successful_rollback(tmp_path, trigger):
    result = finish_install_case(tmp_path, trigger=trigger, exit_code=23)
    assert result.unit.read_bytes() == PRIOR_UNIT and result.config.read_bytes() == PRIOR_CONFIG
    assert result.calls == ["daemon-reload", "enable", "restart"]
    assert not result.backup.exists()


@pytest.mark.parametrize("enabled", [0, 1])
@pytest.mark.parametrize("failure", ["unit", "config", "daemon-reload", "enabled-state", "stop"])
def test_pre_activation_restore_preserves_inactive_service(tmp_path, enabled, failure):
    kwargs = (
        {"fail_copy": failure}
        if failure in {"unit", "config"}
        else {"fail_service": ("enable" if enabled else "disable") if failure == "enabled-state" else failure}
    )
    result = finish_install_case(tmp_path, service_flags=(0, enabled), **kwargs)
    require_backup_retained(result)
    assert result.calls[-1] == "stop"
    assert "restart" not in result.calls
    assert result.service_state[0] == 0
    if failure in {"unit", "config"}:
        assert result.calls == ["stop"]


@pytest.mark.parametrize("enabled", [0, 1])
@pytest.mark.parametrize("failure", ["unit", "config", "daemon-reload", "enabled-state"])
def test_pre_activation_restore_never_restarts_unconfirmed_files(tmp_path, enabled, failure):
    kwargs = (
        {"fail_copy": failure}
        if failure in {"unit", "config"}
        else {"fail_service": ("enable" if enabled else "disable") if failure == "enabled-state" else failure}
    )
    result = finish_install_case(tmp_path, service_flags=(1, enabled), **kwargs)
    require_backup_retained(result)
    assert "restart" not in result.calls and "stop" not in result.calls
    assert result.service_state[0] == 1


@pytest.mark.parametrize("active,enabled", [(0, 0), (0, 1), (1, 0), (1, 1)])
@pytest.mark.parametrize("trigger", ["candidate_health", "candidate_activation"])
def test_failure_after_activation_never_restores_old_writer(tmp_path, active, enabled, trigger):
    result = finish_install_case(tmp_path, trigger=trigger, service_flags=(active, enabled))
    assert result.unit.read_bytes() == CANDIDATE_UNIT and result.config.read_bytes() == CANDIDATE_CONFIG
    assert result.service_state == (1, 1)
    assert result.calls == []
    assert result.backup.exists()
    assert (result.backup / "unit").read_bytes() == PRIOR_UNIT
    assert (result.backup / "config").read_bytes() == PRIOR_CONFIG
    assert "manager_automation_installer_recovery_required=true" in result.process.stderr
    assert "rollback_attempted" not in result.process.stderr
