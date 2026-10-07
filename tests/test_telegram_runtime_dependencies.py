from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_owner_incoming_control_initializes_without_manager_site_packages() -> None:
    probe = """
import asyncio
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from autostop_manager.diagnostics import audit_documentation, instruction_paths
from autostop_manager.telegram_bridge import InboundMonitor
from autostop_manager.telegram_automation_control import (
    TelegramAutomationIncomingCoordinator,
    TelegramAutomationMessage,
    build_runtime_owner_adapter,
)

assert 'markdown_it' not in sys.modules
assert 'autostop_manager.markdown_links' not in sys.modules
assert 'AGENTS.md' in instruction_paths(Path(sys.argv[1]))
assert audit_documentation(Path(sys.argv[1]), check_external_links=False)['ok'] is False
monitor = InboundMonitor()
adapter = build_runtime_owner_adapter(
    owner_peer_id=1,
    socket_path=Path('/nonexistent/import-probe.sock'),
    idempotency_secret=b'0' * 32,
)
coordinator = TelegramAutomationIncomingCoordinator(adapter)

async def reply(*args):
    raise AssertionError('A non-owner message must not trigger a reply.')

assert asyncio.run(coordinator.route(
    TelegramAutomationMessage(peer_id=2, message_id=1, text='/automations', is_private=True),
    reply=reply,
)) is False
print('owner_incoming_dependency_boundary=ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", probe, str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "owner_incoming_dependency_boundary=ok"


def test_installer_checks_real_owner_lane_before_accepting_dependencies(tmp_path: Path) -> None:
    installer = (ROOT / "scripts/install-telegram-bridge.sh").read_text(encoding="utf-8")
    function = re.search(r"(?ms)^validate_bridge_imports\(\) \{\n.*?^\}", installer)
    assert function is not None
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -S "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    environment = {**os.environ, "venv_root": str(python.parent.parent), "PROJECT_ROOT": str(ROOT)}

    result = subprocess.run(
        ["bash", "-c", function.group() + "\nvalidate_bridge_imports"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    reused = installer.index(
        'if [[ "${account}" == "work" && "${work_candidate_reused}" -eq 1 ]]; then',
        installer.index('if [[ "${account}" == "work" && "${work_candidate_reused}" -eq 1 ]]; then') + 1,
    )
    assert installer.index("if ! validate_bridge_imports; then", reused) < installer.index(
        'echo "telegram_bridge_installed=true"', reused
    )
    fresh = installer.index('if ! "${venv_root}/bin/python" -m pip check; then')
    assert installer.index("if ! validate_bridge_imports; then", fresh) < installer.index(
        "if ! finalize_work_candidate_runtime; then", fresh
    )
