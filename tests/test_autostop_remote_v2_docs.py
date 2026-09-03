from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.knowledge_base import plan_command_routes


ROOT = Path(__file__).resolve().parents[1]


def _workflows(query: str) -> list[str]:
    return [route["workflow_id"] for route in plan_command_routes(query)]


def test_autostop_remote_v2_playbook_and_skill_keep_cli_only_fleet_gate():
    playbook = (ROOT / "docs" / "agent" / "autostop_remote_v2_playbook.md").read_text(encoding="utf-8")
    skill = (ROOT / ".agents" / "skills" / "manage-autostop-remote-v2" / "SKILL.md").read_text(encoding="utf-8")

    for expected in (
        "managed-pc list",
        "managed-pc fleet-health",
        "managed-pc status <alias>",
        "exact canonical alias",
        "substring",
        "Windows hostname",
        "listener port",
        "codex-run",
        "powershell",
        "cmd",
        "copy-to",
        "copy-from",
        "rename",
        "revoke",
        "scripts/codex_home_pc_bootstrap.ps1",
        "system Windows SSHD",
        "Windows firewall",
        "StrictHostKeyChecking=accept-new",
        "FastMCP",
        "mcp_server.py",
        "mcp_tools.py",
        "public listener",
    ):
        assert expected in playbook

    for expected in (
        "root-owned local `managed-pc` CLI",
        "managed-pc list",
        "managed-pc fleet-health",
        "managed-pc status <alias>",
        "StrictHostKeyChecking=accept-new",
        "scripts/codex_home_pc_bootstrap.ps1",
    ):
        assert expected in skill


def test_autostop_remote_v2_command_routes_separate_observation_from_actions():
    payload = json.loads((ROOT / "docs" / "agent" / "command_routes.json").read_text(encoding="utf-8"))
    routes = {route["command_id"]: route for route in payload["routes"]}
    observation = routes["remote_codex_access"]
    action = routes["remote_codex_access_change"]

    assert "managed-pc list" in observation["signals"]["phrases"]
    assert "codex-run" in observation["signals"]["exclude"]
    assert "codex-run" in action["signals"]["all"][1]
    assert _workflows("Покажи список управляемых Windows v2") == ["remote_codex_access"]
    assert _workflows("Проверь статус ресепшена") == ["remote_codex_access"]
    assert _workflows("Запусти codex-run на managed-pc") == ["remote_codex_access_change"]

    for query in (
        "Запусти Codex на ресепшене",
        "Запусти кодекс на компьютере механиков",
        "Выполни PowerShell на компьютере механиков",
        "Выполни cmd в офисе",
        "Передай файл в бокс",
        "Переименуй компьютер в боксе",
        "Отзови компьютер механика",
    ):
        assert _workflows(query) == ["remote_codex_access_change"]

    # Server infrastructure is not a managed-Windows action. It must not pick
    # the v2 fleet playbook merely because the query mentions SSHD.
    assert _workflows("Перезапусти SSHD на удалённом сервере") == []
    assert _workflows("Перезагрузи компьютер в боксе") == []
