"""Local release checks; live integrations are an explicit, separate scope."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

TEXT_DOCUMENTS = (
    "AGENTS.md",
    ".agents/skills/manage-owner-telegram/SKILL.md",
    ".agents/skills/manage-autostop-store/SKILL.md",
    ".agents/skills/manage-fst-vpn/SKILL.md",
    "docs/agent/operations.md",
    "docs/agent/deployment_runbook.md",
    "docs/agent/j1_web_research.md",
)
# Aggregate ceiling for the active operational instruction documents.  It
# remains bounded while allowing their current guarded-workflow coverage.
INSTRUCTION_BUDGET_BYTES = 32 * 1024


def audit_documentation(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Validate source documents without opening or creating a Manager database."""
    root = root.resolve()
    warnings: list[str] = []
    actual = {"AGENTS.md"}
    actual.update(str(p.relative_to(root)) for p in (root / "docs/agent").rglob("*.md"))
    actual.update(str(p.relative_to(root)) for p in (root / ".agents/skills").rglob("*.md"))
    if actual != set(TEXT_DOCUMENTS):
        warnings.append("instruction_inventory_mismatch")
    size = 0
    for name in TEXT_DOCUMENTS:
        path = root / name
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("outside_project")
            text = path.read_text(encoding="utf-8")
            size += len(text.encode())
            if not text.strip():
                warnings.append(f"empty_document:{name}")
            if name.endswith("SKILL.md"):
                header = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
                fields = header.group(1) if header else ""
                if not re.search(r"(?m)^name: " + re.escape(path.parent.name) + r"$", fields):
                    warnings.append(f"skill_name_invalid:{name}")
                if not re.search(r"(?m)^description: .+", fields):
                    warnings.append(f"skill_description_missing:{name}")
            for link in re.findall(r"\]\(([^)]+)\)", text):
                target = link.split("#", 1)[0]
                if not target or re.match(r"^[a-zA-Z]+://", target):
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    warnings.append(f"document_link_invalid:{name}")
        except (OSError, UnicodeError, ValueError):
            warnings.append(f"document_unreadable:{name}")
    if size > INSTRUCTION_BUDGET_BYTES:
        warnings.append("instruction_budget_exceeded")
    return {"ok": not warnings, "documents": len(TEXT_DOCUMENTS), "bytes": size, "warnings": warnings}


def watchdog_status() -> dict[str, Any]:
    """Legacy watchdog units must be absent; never change them here."""
    checks = {}
    for suffix in ("timer", "service"):
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    f"autostopcrm-watchdog.{suffix}",
                    "--property=LoadState",
                    "--value",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            checks[suffix] = result.returncode == 0 and result.stdout.strip() == "not-found"
        except (OSError, subprocess.TimeoutExpired):
            checks[suffix] = False
    return {"ok": all(checks.values()), "desired_state": "absent", "checks": checks}


def diagnose(*, integrations: bool = False, full: bool = False) -> dict[str, Any]:
    from mcp.server.fastmcp import FastMCP

    from .mcp_contract import validate_manager_mcp_surface
    from .mcp_tools import register_manager_tools

    server = FastMCP("AutoStop-local-check")
    register_manager_tools(server)
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    checks = {"documentation": audit_documentation(), "mcp": validate_manager_mcp_surface(schemas)}
    if integrations:
        from .integration_audit import build_integration_audit

        checks["integrations"] = build_integration_audit(full=full)
        checks["watchdog"] = watchdog_status()
    elif full:
        checks["scope"] = {"ok": False, "warnings": ["full_requires_integrations"]}
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
        "warnings": [name for name, check in checks.items() if not check["ok"]],
    }
