from __future__ import annotations

from pathlib import Path


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
