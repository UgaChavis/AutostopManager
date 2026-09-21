import os
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
    assert "Group=autostop-automation" in unit
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
    assert 'GROUP_HELPER="${PROJECT_ROOT}/scripts/ensure-automation-group.sh"' in source
    assert source.index('"${GROUP_HELPER}" >/dev/null') < source.index("install -d -o root -g root -m 0700")
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


def _automation_group_helper_fixture(tmp_path: Path, group_lines: list[str]):
    database = tmp_path / "group"
    database.write_text("".join(f"{line}\n" for line in group_lines), encoding="utf-8")
    calls = tmp_path / "groupadd.calls"
    fake_getent = tmp_path / "getent"
    fake_getent.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        '[[ "$1" == group ]]\n'
        'key="$2"\n'
        "while IFS= read -r line; do\n"
        '  name="${line%%:*}"\n'
        '  remainder="${line#*:}"\n'
        '  remainder="${remainder#*:}"\n'
        '  gid="${remainder%%:*}"\n'
        '  if [[ "${key}" == "${name}" || "${key}" == "${gid}" ]]; then printf "%s\\n" "${line}"; exit 0; fi\n'
        'done < "$FAKE_GROUP_DATABASE"\n'
        "exit 2\n",
        encoding="utf-8",
    )
    fake_groupadd = tmp_path / "groupadd"
    fake_groupadd.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf "%s\\n" "$*" >> "$FAKE_GROUPADD_CALLS"\n'
        '[[ "$*" == "--system --gid 10001 autostop-automation" ]]\n'
        'printf "autostop-automation:x:10001:\\n" >> "$FAKE_GROUP_DATABASE"\n',
        encoding="utf-8",
    )
    fake_getent.chmod(0o755)
    fake_groupadd.chmod(0o755)

    source = (ROOT / "scripts/ensure-automation-group.sh").read_text(encoding="utf-8")
    source = source.replace('if [[ "${EUID}" -ne 0 ]]', "if false")
    source = source.replace('getent_bin="/usr/bin/getent"', f'getent_bin="{fake_getent}"')
    source = source.replace('groupadd_bin="/usr/sbin/groupadd"', f'groupadd_bin="{fake_groupadd}"')
    source = source.replace('lock_path="/run/autostop-automation-group.lock"', f'lock_path="{tmp_path / "group.lock"}"')
    helper = tmp_path / "ensure-automation-group.sh"
    helper.write_text(source, encoding="utf-8")
    helper.chmod(0o755)
    environment = {
        **os.environ,
        "FAKE_GROUP_DATABASE": str(database),
        "FAKE_GROUPADD_CALLS": str(calls),
    }
    return helper, database, calls, environment


def test_automation_group_helper_creates_and_reuses_exact_group(tmp_path: Path):
    helper, database, calls, environment = _automation_group_helper_fixture(tmp_path, [])

    first = subprocess.run([str(helper)], capture_output=True, text=True, env=environment, timeout=10)
    second = subprocess.run([str(helper)], capture_output=True, text=True, env=environment, timeout=10)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout == "automation_group_ready=true\n"
    assert database.read_text(encoding="utf-8") == "autostop-automation:x:10001:\n"
    assert calls.read_text(encoding="utf-8").splitlines() == ["--system --gid 10001 autostop-automation"]


def test_automation_group_helper_fails_closed_on_name_conflict(tmp_path: Path):
    helper, _database, calls, environment = _automation_group_helper_fixture(tmp_path, ["autostop-automation:x:20000:"])

    result = subprocess.run([str(helper)], capture_output=True, text=True, env=environment, timeout=10)

    assert result.returncode == 1
    assert result.stderr == "automation_group_name_conflict=true\n"
    assert not calls.exists()


def test_automation_group_helper_fails_closed_on_gid_conflict(tmp_path: Path):
    helper, _database, calls, environment = _automation_group_helper_fixture(tmp_path, ["some-other-group:x:10001:"])

    result = subprocess.run([str(helper)], capture_output=True, text=True, env=environment, timeout=10)

    assert result.returncode == 1
    assert result.stderr == "automation_group_gid_conflict=true\n"
    assert not calls.exists()
