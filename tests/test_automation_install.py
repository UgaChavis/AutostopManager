from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_automation_unit_is_local_only_and_hardened():
    unit = (ROOT / "deploy/systemd/autostop-manager-scheduler.service").read_text(encoding="utf-8")

    assert "Type=notify" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "AUTOSTOP_AUTOMATION_DB=/var/lib/autostop-manager-scheduler/registry.sqlite3" in unit
    assert "StateDirectory=autostop-manager-scheduler" in unit
    assert "AUTOSTOP_AUTOMATION_CONTROL_SOCKET=/run/autostop-manager-automation/control.sock" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
    assert "ProtectSystem=strict" in unit
    assert 'UNIT_NAME="autostop-manager-scheduler.service"' in (
        ROOT / "scripts/install-manager-automation.sh"
    ).read_text(encoding="utf-8")


def test_automation_installer_is_parseable_and_activation_is_opt_in():
    script_path = ROOT / "scripts/install-manager-automation.sh"
    source = script_path.read_text(encoding="utf-8")

    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert "activation_requested=" in source
    assert source.index('if [[ "${activate}" -ne 1 ]]') < source.index("systemctl enable --now")
    assert "AUTOSTOP_AUTOMATION_CONTROL_ALLOWED_UIDS" in source
    assert "AUTOSTOP_AUTOMATION_CONTROL_GID=10001" in source
    assert "--crm-revision" in source
    assert "crm_revision_required_for_activation=true" in source
    assert "AUTOSTOP_AUTOMATION_CRM_REVISION" in source
    assert "AUTOSTOP_AUTOMATION_CRM_VERSION" in source
    assert source.index('rollback_dir="$(mktemp -d') < source.index("automation_release hold")
    assert "manager_automation_installer_rollback_attempted=true" in source
    assert source.index("automation_release hold") < source.index("install -o root -g root -m 0600")


def test_automation_installer_requires_crm_revision_before_activation(tmp_path: Path):
    script_path = ROOT / "scripts/install-manager-automation.sh"

    result = subprocess.run(
        [
            "bash",
            str(script_path),
            "--activate",
            "--manager-revision",
            "a" * 40,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "crm_revision_required_for_activation=true" in result.stderr
