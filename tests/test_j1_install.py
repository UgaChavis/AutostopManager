from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_j1_worker_unit_shares_mcp_cache_owner_and_is_cache_scoped() -> None:
    unit = (ROOT / "deploy/systemd/autostop-j1.service").read_text(encoding="utf-8")

    assert "User=root" in unit
    assert "Group=root" in unit
    assert "InaccessiblePaths=-/opt/AutostopManager/.env" in unit
    assert "CacheDirectory=autostop-j1" in unit
    assert "CacheDirectoryMode=0700" in unit
    assert "ReadWritePaths=/var/cache/autostop-j1" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=yes" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=CAP_SETUID CAP_SETGID" in unit
    assert "AmbientCapabilities=CAP_SETUID CAP_SETGID" in unit
    assert "MemoryMax=384M" in unit
    assert "TasksMax=32" in unit
    assert "Environment=AUTOSTOP_J1_SEARXNG_URL=http://127.0.0.1:8890" in unit
    assert "ExecStart=/usr/bin/python3 -m autostop_manager.j1_research worker" in unit
    assert "EnvironmentFile=" not in unit
    assert "ReadWritePaths=/opt/AutostopManager" not in unit


def test_manager_mcp_can_queue_j1_in_the_shared_cache() -> None:
    unit = (ROOT / "deploy/systemd/autostop-manager-mcp.service").read_text(encoding="utf-8")

    assert "User=root" in unit
    assert "Group=root" in unit
    assert "CacheDirectory=autostop-j1" in unit
    assert "CacheDirectoryMode=0700" in unit
    assert "Environment=AUTOSTOP_J1_CACHE_DIR=/var/cache/autostop-j1" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/opt/AutostopManager/data /var/cache/autostop-j1" in unit


def test_j1_installer_probes_after_activation() -> None:
    installer = (ROOT / "scripts/install-j1-worker.sh").read_text(encoding="utf-8")

    assert "j1_current_release_source_required=true" in installer
    assert "j1_replace_unit_required=true" in installer
    assert "systemd-analyze verify" in installer
    assert "-m autostop_manager.j1_research probe" in installer
    assert installer.index('systemctl start "${UNIT_NAME}"') < installer.index("-m autostop_manager.j1_research probe")
    assert installer.index("-m autostop_manager.j1_research probe") < installer.index("j1_worker_active=true")


def test_j1_browser_installer_validates_helpers_and_exact_revision_images() -> None:
    installer = (ROOT / "scripts/install-j1-browser-stack.sh").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/run-j1-browser-stack.sh").read_text(encoding="utf-8")

    assert '! -x "${STACK_RUNNER}" || -L "${STACK_RUNNER}"' in installer
    assert '! -x "${ATTESTER}" || -L "${ATTESTER}"' in installer
    assert 'revision="$("${STACK_RUNNER}" revision)"' in installer
    assert 'renderer_image="autostop-j1-browser-renderer:${revision}"' in installer
    assert 'proxy_image="autostop-j1-browser-proxy:${revision}"' in installer
    assert 'docker image inspect "${renderer_image}" "${proxy_image}"' in installer
    assert "org.opencontainers.image.revision" in installer
    assert "j1_browser_image_revision_invalid=true" in installer
    assert 'REVISION_FILE="${RELEASE_ROOT}/REVISION"' in runner
    assert '[[ ! "${revision_lines[0]}" =~ ^[0-9a-f]{40,64}$ ]]' in runner
    assert 'export AUTOSTOP_J1_BROWSER_IMAGE_REVISION="${revision}"' in runner


@pytest.mark.skipif(os.geteuid() != 0, reason="installer root gate")
@pytest.mark.parametrize("attester_succeeds", [True, False])
def test_j1_browser_standalone_verify_reattests_and_fails_closed(tmp_path: Path, attester_succeeds: bool) -> None:
    project = tmp_path / "release"
    scripts = project / "scripts"
    systemd = project / "deploy" / "systemd"
    browser = project / "deploy" / "j1-browser"
    package = project / "autostop_manager"
    for directory in (scripts, systemd, browser, package):
        directory.mkdir(parents=True, exist_ok=True)

    unit_name = "autostop-j1-browser.service"
    (systemd / unit_name).write_text("[Unit]\nDescription=test\n", encoding="utf-8")
    (browser / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (browser / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (package / "j1_browser_verify.py").write_text("# verifier sentinel\n", encoding="utf-8")

    revision = "a" * 40
    runner = scripts / "run-j1-browser-stack.sh"
    runner.write_text(f"#!/bin/sh\nprintf '%s\\n' '{revision}'\n", encoding="utf-8")
    runner.chmod(0o755)

    marker_dir = tmp_path / "attestation"
    marker_dir.mkdir()
    marker = marker_dir / "isolation-ready"
    marker.write_text("stale\n", encoding="utf-8")
    call_log = tmp_path / "calls.log"
    attester = scripts / "attest-j1-browser-stack.sh"
    attester.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ ! -e "$FAKE_MARKER" ]\n'
        "printf 'attest\\n' >> \"$FAKE_CALL_LOG\"\n"
        'printf \'revision=%s\\n\' "$FAKE_REVISION" > "$FAKE_MARKER"\n'
        + ("exit 0\n" if attester_succeeds else "exit 7\n"),
        encoding="utf-8",
    )
    attester.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "systemctl").write_text(
        '#!/bin/sh\nprintf \'systemctl:%s\\n\' "$*" >> "$FAKE_CALL_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    (fake_bin / "systemd-analyze").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = -c ]; then exec "$REAL_PYTHON" "$@"; fi\n'
        "printf 'probe\\n' >> \"$FAKE_CALL_LOG\"\n"
        '[ -f "$FAKE_MARKER" ]\n'
        'printf \'%s\\n\' \'{"ok":true,"browser_ready":true,"browser_containers_ready":true}\'\n',
        encoding="utf-8",
    )
    for executable in fake_bin.iterdir():
        executable.chmod(0o755)

    source = (ROOT / "scripts/install-j1-browser-stack.sh").read_text(encoding="utf-8")
    replacements = {
        'RELEASE_ROOT="/opt/autostop-manager-releases/current"': f'RELEASE_ROOT="{project}"',
        'RUNTIME_PYTHON="/usr/bin/python3"': f'RUNTIME_PYTHON="{fake_python}"',
        'UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"': f'UNIT_PATH="{tmp_path / unit_name}"',
        'SOCKET_DIR="/run/autostop-j1-browser"': f'SOCKET_DIR="{tmp_path / "socket"}"',
        'ATTESTATION_DIR="/run/autostop-j1-browser-attestation"': f'ATTESTATION_DIR="{marker_dir}"',
    }
    for old, new in replacements.items():
        assert old in source
        source = source.replace(old, new, 1)
    installer = scripts / "install-j1-browser-stack.sh"
    installer.write_text(source, encoding="utf-8")
    installer.chmod(0o755)

    completed = subprocess.run(
        [str(installer), "--verify"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_CALL_LOG": str(call_log),
            "FAKE_MARKER": str(marker),
            "FAKE_REVISION": revision,
            "REAL_PYTHON": sys.executable,
        },
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls.count("attest") == 1
    if attester_succeeds:
        assert completed.returncode == 0, completed.stderr
        assert calls.index("attest") < calls.index("probe")
        assert marker.read_text(encoding="utf-8") == f"revision={revision}\n"
        assert "j1_browser_isolation_attested=true" in completed.stdout
    else:
        assert completed.returncode == 1
        assert "j1_browser_attestation_failed=true" in completed.stderr
        assert "probe" not in calls
        assert not marker.exists()
