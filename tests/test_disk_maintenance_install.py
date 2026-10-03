from __future__ import annotations

import json
import os
import stat
import subprocess
from copy import deepcopy

import pytest

from scripts.disk_maintenance import install as installer
from scripts.disk_maintenance.core import DEFAULT_POLICY
from scripts.disk_maintenance.util import MaintenanceError


@pytest.fixture
def source(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("native installer ownership fixtures require root")
    root = tmp_path / "source"
    root.mkdir()
    for name in (
        *installer.PAYLOAD,
        *installer.INSTALL_SOURCE,
        *("deploy/systemd/" + item for item in installer.UNITS),
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# frozen test payload\n")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "user.name", "Fixture"],
        ["add", "."],
        ["commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, revision


@pytest.fixture
def target(tmp_path, monkeypatch):
    root = tmp_path / "system"
    root.mkdir()
    for name, relative in (
        ("BASE", "libexec"),
        ("POLICY", "etc/policy.json"),
        ("STATE", "state"),
        ("WRAPPER", "sbin/helper"),
        ("SYSTEMD", "systemd"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(installer, name, path)
    installer.SYSTEMD.mkdir()
    policy = tmp_path / "approved" / "policy.json"
    policy.parent.mkdir(mode=0o700)
    approved = deepcopy(DEFAULT_POLICY)
    approved["state_root"] = str(installer.STATE)
    policy.write_text(json.dumps(approved))
    policy.chmod(0o600)
    receipt = tmp_path / "receipt" / "install.json"
    receipt.parent.mkdir(mode=0o700)
    actual = installer._command
    calls = []
    states = {name: {"enabled": "not-found", "active": "inactive"} for name in installer.TIMERS}

    def command(argv, *, allow_failure=False):
        calls.append(argv)
        if argv[0] == "git":
            return actual(argv, allow_failure=allow_failure)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return states[argv[-1]]["enabled"]
        if argv[:2] == ["systemctl", "is-active"]:
            return states[argv[-1]]["active"]
        if argv[:2] == ["systemctl", "daemon-reload"]:
            for name, state in states.items():
                if not (installer.SYSTEMD / name).exists():
                    state["enabled"] = "not-found"
                elif state["enabled"] == "not-found":
                    state["enabled"] = "disabled"
        if argv[:2] == ["systemctl", "enable"]:
            states[argv[-1]]["enabled"] = "enabled-runtime" if "--runtime" in argv else "enabled"
            if "--now" in argv:
                states[argv[-1]]["active"] = "active"
        if argv[:2] == ["systemctl", "disable"]:
            states[argv[-1]]["enabled"] = "disabled" if (installer.SYSTEMD / argv[-1]).exists() else "not-found"
        if argv[:2] == ["systemctl", "start"]:
            states[argv[-1]]["active"] = "active"
        if argv[:2] == ["systemctl", "stop"]:
            states[argv[-1]]["active"] = "inactive"
        return ""

    command.states = states
    monkeypatch.setattr(installer, "_command", command)
    return policy, receipt, calls, command


def test_install_freezes_only_committed_payload_and_private_policy(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    result = installer.install(root, policy, revision, receipt)
    assert result["status"] == "installed"
    current = installer.BASE / "current"
    assert current.is_symlink()
    for name in installer.PAYLOAD:
        assert (current / name.removeprefix("scripts/")).read_bytes() == (root / name).read_bytes()
    assert stat.S_IMODE(installer.POLICY.stat().st_mode) == 0o600
    assert stat.S_IMODE(installer.POLICY.parent.stat().st_mode) == 0o700
    assert installer.WRAPPER.read_bytes() == installer.WRAPPER_BYTES
    assert all(not (call[0] == "systemctl" and "restart" in call) for call in calls)
    assert json.loads(receipt.read_text())["status"] == "installed"


def test_dirty_selected_source_never_installs(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    (root / installer.PAYLOAD[0]).write_text("changed after tests\n")
    with pytest.raises(MaintenanceError, match="install_source_dirty"):
        installer.install(root, policy, revision, receipt)
    assert not installer.WRAPPER.exists()
    assert not any(call[0] == "systemctl" for call in calls)


def test_failed_activation_restores_existing_policy_and_never_enables_apps(source, target, monkeypatch):
    root, revision = source
    policy, receipt, calls, command = target
    installer.POLICY.write_bytes(b"original policy")
    installer.POLICY.chmod(0o600)

    def fail_verify(argv, *, allow_failure=False):
        if argv[0] == "systemd-analyze":
            raise MaintenanceError("injected_verify_failure")
        return command(argv, allow_failure=allow_failure)

    monkeypatch.setattr(installer, "_command", fail_verify)
    with pytest.raises(MaintenanceError, match="install_failed_rolled_back"):
        installer.install(root, policy, revision, receipt)
    assert installer.POLICY.read_bytes() == b"original policy"
    assert not installer.WRAPPER.exists()
    assert not (installer.BASE / "current").is_symlink()
    assert json.loads(receipt.read_text())["status"] == "rolled_back"
    assert not any(call[:2] == ["systemctl", "enable"] for call in calls)


def test_symlink_config_target_is_rejected_before_activation(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    protected = installer.POLICY.parent / "keep"
    protected.write_bytes(b"protected")
    os.symlink(protected, installer.POLICY)
    with pytest.raises(MaintenanceError, match="install_previous_file_invalid"):
        installer.install(root, policy, revision, receipt)
    assert protected.read_bytes() == b"protected"
    assert not any(call[:2] == ["systemctl", "enable"] for call in calls)


def test_conflicting_current_directory_keeps_system_config(source, target):
    root, revision = source
    policy, receipt, _, _ = target
    installer.BASE.mkdir()
    (installer.BASE / "current").mkdir()
    with pytest.raises(MaintenanceError, match="install_current_invalid"):
        installer.install(root, policy, revision, receipt)
    assert not installer.POLICY.exists()
    assert not installer.WRAPPER.exists()


def test_invalid_full_policy_rejected_before_install_mutation(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    payload = json.loads(policy.read_text())
    del payload["delete_budget_seconds"]
    policy.write_text(json.dumps(payload))
    with pytest.raises(MaintenanceError, match="policy_budget_invalid"):
        installer.install(root, policy, revision, receipt)
    assert not installer.BASE.exists() and not installer.STATE.exists()
    assert not installer.POLICY.exists() and not installer.WRAPPER.exists()
    assert not any(call[0] == "systemctl" for call in calls)


def test_invalid_ci_target_rejected_before_install_mutation(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    payload = json.loads(policy.read_text())
    payload["ci"]["repository"] = "unapproved/repo"
    policy.write_text(json.dumps(payload))
    with pytest.raises(MaintenanceError, match="ci_target_invalid"):
        installer.install(root, policy, revision, receipt)
    assert not installer.BASE.exists()
    assert not any(call[0] == "systemctl" for call in calls)


def test_dirty_installer_itself_is_rejected_before_system_mutation(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    (root / installer.INSTALL_SOURCE[1]).write_text("changed installer\n")
    with pytest.raises(MaintenanceError, match="install_source_dirty"):
        installer.install(root, policy, revision, receipt)
    assert not installer.BASE.exists()
    assert not any(call[0] == "systemctl" for call in calls)


@pytest.mark.parametrize(
    "enabled,active",
    [
        ("enabled", "active"),
        ("enabled", "inactive"),
        ("enabled-runtime", "active"),
        ("disabled", "active"),
        ("disabled", "inactive"),
        ("not-found", "inactive"),
    ],
)
def test_partial_timer_adoption_restores_exact_original_lifecycle(source, target, monkeypatch, enabled, active):
    root, revision = source
    policy, receipt, _, command = target
    if enabled != "not-found":
        for name in installer.TIMERS:
            (installer.SYSTEMD / name).write_text("old timer config\n")
    for value in command.states.values():
        value.update(enabled=enabled, active=active)

    def partial_enable(argv, *, allow_failure=False):
        result = command(argv, allow_failure=allow_failure)
        if argv[:3] == ["systemctl", "enable", "--now"] and argv[-1] == installer.TIMERS[1]:
            command.states[argv[-1]]["active"] = "inactive"
            raise MaintenanceError("injected_partial_enable")
        return result

    monkeypatch.setattr(installer, "_command", partial_enable)
    with pytest.raises(MaintenanceError, match="install_failed_rolled_back"):
        installer.install(root, policy, revision, receipt)
    assert all(value == {"enabled": enabled, "active": active} for value in command.states.values())
    saved = json.loads(receipt.read_text())
    assert saved["status"] == "rolled_back" and saved["rollback_errors"] == []
    assert not installer.WRAPPER.exists() and not installer.POLICY.exists()


def test_rollback_stop_error_still_restores_all_files_and_other_timer(source, target, monkeypatch):
    root, revision = source
    policy, receipt, calls, command = target
    installer.POLICY.write_bytes(b"original technical policy")
    installer.POLICY.chmod(0o600)

    def failed_actions(argv, *, allow_failure=False):
        if argv[:3] == ["systemctl", "enable", "--now"] and argv[-1] == installer.TIMERS[1]:
            raise MaintenanceError("injected_adoption_failure")
        if argv[:2] == ["systemctl", "stop"] and argv[-1] == installer.TIMERS[0]:
            calls.append(argv)
            raise MaintenanceError("injected_stop_failure")
        return command(argv, allow_failure=allow_failure)

    monkeypatch.setattr(installer, "_command", failed_actions)
    with pytest.raises(MaintenanceError, match="install_rollback_incomplete"):
        installer.install(root, policy, revision, receipt)
    assert installer.POLICY.read_bytes() == b"original technical policy"
    assert not installer.WRAPPER.exists() and not (installer.BASE / "current").is_symlink()
    assert all(not (installer.SYSTEMD / name).exists() for name in installer.UNITS)
    assert command.states[installer.TIMERS[1]] == {"enabled": "not-found", "active": "inactive"}
    saved = json.loads(receipt.read_text())
    assert saved["status"] == "rollback_required" and "rollback_timer_stop_failed" in saved["rollback_errors"]


def test_failed_file_restore_does_not_reactivate_timers_with_partial_configuration(source, target, monkeypatch):
    root, revision = source
    policy, receipt, calls, command = target
    for name in installer.TIMERS:
        (installer.SYSTEMD / name).write_text("old timer config\n")
        command.states[name] = {"enabled": "enabled", "active": "active"}

    def fail_verify(argv, *, allow_failure=False):
        if argv[0] == "systemd-analyze":
            raise MaintenanceError("injected_verify_failure")
        return command(argv, allow_failure=allow_failure)

    actual_restore = installer._restore_file

    def fail_one(item):
        if item["path"] == str(installer.SYSTEMD / installer.TIMERS[0]):
            raise MaintenanceError("injected_restore_failure")
        actual_restore(item)

    monkeypatch.setattr(installer, "_command", fail_verify)
    monkeypatch.setattr(installer, "_restore_file", fail_one)
    with pytest.raises(MaintenanceError, match="install_rollback_incomplete"):
        installer.install(root, policy, revision, receipt)
    assert all(value["active"] == "inactive" for value in command.states.values())
    assert not any(call[:2] == ["systemctl", "start"] for call in calls)
    saved = json.loads(receipt.read_text())
    assert "rollback_file_failed" in saved["rollback_errors"]
    assert "rollback_activation_skipped" in saved["rollback_errors"]


@pytest.mark.parametrize(
    "kind", ["package-owner", "child-owner", "package-writable", "child-writable", "child-symlink"]
)
def test_existing_package_directories_must_be_trusted(source, target, monkeypatch, kind):
    root, revision = source
    policy, receipt, calls, _ = target
    installer.install(root, policy, revision, receipt)
    package = (installer.BASE / "current").resolve()
    child = package / "disk_maintenance"
    if kind == "package-owner":
        os.chown(package, 4242, 0)
    elif kind == "child-owner":
        os.chown(child, 4242, 0)
    elif kind == "package-writable":
        package.chmod(0o777)
    elif kind == "child-writable":
        child.chmod(0o777)
    else:
        preserved = package / "preserved-child"
        child.rename(preserved)
        child.symlink_to(preserved)
    second_receipt = receipt.parent / "second.json"
    count = len(calls)
    with pytest.raises(MaintenanceError, match="install_package_directory_invalid"):
        installer.install(root, policy, revision, second_receipt)
    assert not second_receipt.exists()
    assert not any(call[0] == "systemctl" for call in calls[count:])


def test_existing_package_symlink_is_rejected_without_following_it(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    installer.install(root, policy, revision, receipt)
    package = (installer.BASE / "current").resolve()
    preserved = package.parent / "preserved-package"
    package.rename(preserved)
    package.symlink_to(preserved)
    count = len(calls)
    with pytest.raises(MaintenanceError, match="install_package_directory_invalid"):
        installer.install(root, policy, revision, receipt.parent / "second.json")
    assert (preserved / "autostop-disk-maintenance.py").read_bytes() == (root / installer.PAYLOAD[0]).read_bytes()
    assert not any(call[0] == "systemctl" for call in calls[count:])


def test_unsupported_original_timer_state_rejected_before_config_adoption(source, target):
    root, revision = source
    policy, receipt, calls, command = target
    command.states[installer.TIMERS[0]]["enabled"] = "masked-runtime"
    with pytest.raises(MaintenanceError, match="install_timer_lifecycle_unsupported"):
        installer.install(root, policy, revision, receipt)
    assert not installer.POLICY.exists() and not installer.WRAPPER.exists()
    assert not any(call[:2] == ["systemctl", "enable"] for call in calls)


def test_original_file_group_mode_and_bytes_are_restored(source, target, monkeypatch):
    root, revision = source
    policy, receipt, _, command = target
    installer.POLICY.write_bytes(b"original policy")
    installer.POLICY.chmod(0o600)
    os.chown(installer.POLICY, 0, 4242)

    def fail_verify(argv, *, allow_failure=False):
        if argv[0] == "systemd-analyze":
            raise MaintenanceError("injected_verify_failure")
        return command(argv, allow_failure=allow_failure)

    monkeypatch.setattr(installer, "_command", fail_verify)
    with pytest.raises(MaintenanceError, match="install_failed_rolled_back"):
        installer.install(root, policy, revision, receipt)
    assert installer.POLICY.read_bytes() == b"original policy"
    assert installer.POLICY.stat().st_gid == 4242
    assert stat.S_IMODE(installer.POLICY.stat().st_mode) == 0o600


def test_staging_symlink_is_rejected_before_chmod_or_copy(source, target, monkeypatch, tmp_path):
    root, revision = source
    policy, receipt, _, _ = target
    preserved = tmp_path / "preserved"
    preserved.mkdir(mode=0o700)
    keep = preserved / "keep"
    keep.write_bytes(b"synthetic preserve")
    forged = tmp_path / "forged-staging"
    forged.symlink_to(preserved)
    monkeypatch.setattr(installer.tempfile, "mkdtemp", lambda **_kwargs: str(forged))
    with pytest.raises(MaintenanceError, match="install_package_directory_invalid"):
        installer.install(root, policy, revision, receipt)
    assert stat.S_IMODE(preserved.stat().st_mode) == 0o700
    assert keep.read_bytes() == b"synthetic preserve"
    assert not installer.POLICY.exists() and not installer.WRAPPER.exists()


def test_existing_receipt_preserves_all_backups_and_prevents_reinstall(source, target):
    root, revision = source
    policy, receipt, calls, _ = target
    installer.install(root, policy, revision, receipt)
    prior = {path.name: path.read_bytes() for path in receipt.parent.iterdir() if path.is_file()}
    count = len(calls)
    with pytest.raises(MaintenanceError, match="install_receipt_already_exists"):
        installer.install(root, policy, revision, receipt)
    assert len(calls) == count
    assert {path.name: path.read_bytes() for path in receipt.parent.iterdir() if path.is_file()} == prior
