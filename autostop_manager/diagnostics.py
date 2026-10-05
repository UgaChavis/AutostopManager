"""Local release checks; live integrations are an explicit, separate scope."""

from __future__ import annotations

from os.path import normpath
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .document_links import local_document_link_target

MODULE_PARENTS: dict[str, str | None] = {
    "A1": None,
    "A2": None,
    "A3": None,
    "A4": "A1",
    "A5": "A1",
    "B1": None,
    "B2": None,
    "B3": None,
    "B4": None,
    "C2": None,
    "C3": None,
    "C4": "C3",
    "C5": "C3",
    "C6": "C3",
    "C7": "C3",
    "D1": None,
    "D2": None,
    "D3": None,
    "D4": "D2",
    "D5": "D3",
    "E1": None,
    "E2": None,
    "E3": None,
    "E4": "E1",
    "E5": "E1",
    "E6": "E1",
    "E7": "E1",
    "E8": None,
    "E9": "E1",
    "E10": None,
    "E11": None,
    "F1": None,
    "F2": None,
    "F3": "F2",
    "F4": "F2",
    "F5": "F2",
    "G1": None,
    "H1": None,
    "H2": None,
    "I1": None,
    "I2": None,
    "J1": None,
    "M1": None,
    "M2": "M1",
}
MODULE_DOCUMENTS = {
    module_id: "AGENTS.md" if module_id == "A2" else f"docs/agent/modules/{module_id}.md"
    for module_id in MODULE_PARENTS
}
SKILL_DOCUMENTS = (
    ".agents/skills/manage-owner-telegram/SKILL.md",
    ".agents/skills/manage-autostop-store/SKILL.md",
    ".agents/skills/manage-fst-vpn/SKILL.md",
    ".agents/skills/manage-owner-instagram/SKILL.md",
)
TEXT_DOCUMENTS = ("AGENTS.md", MODULE_DOCUMENTS["A1"], MODULE_DOCUMENTS["M1"], MODULE_DOCUMENTS["M2"], *SKILL_DOCUMENTS)
REFERENCE_DOCUMENTS = tuple(
    path for module_id, path in MODULE_DOCUMENTS.items() if module_id not in {"A1", "A2", "M1", "M2"}
)
# Only the entry instructions and operational skills count toward this budget.
# Detailed module guides and complete plugin catalogs are loaded when needed.
INSTRUCTION_BUDGET_BYTES = 32 * 1024
EXTERNAL_CODEX_ROOTS = (
    Path("/root/.codex/skills"),
    Path("/root/.codex/plugins/cache"),
)
EXTERNAL_SKILL_PACKAGES = frozenset(
    {
        "gmail",
        "windsor-ai",
        "github",
        "build-web-apps",
        "codex-security",
        "openai-developers",
        "plugin-management",
        "visualize",
    }
)
FST_ACCESS_DOCUMENT = Path("/root/.codex/CODEX_VPN_FST_ACCESS.md")
ROLE_JOURNAL_ROOT = Path("/var/lib/autostop-manager/roles/M2")
ROLE_JOURNAL_ENTRIES = {ROLE_JOURNAL_ROOT / "current-state.md", ROLE_JOURNAL_ROOT / "journal/INDEX.md"}


def instruction_paths(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """Include every current Markdown document and missing required instructions."""
    names = set(TEXT_DOCUMENTS) | set(REFERENCE_DOCUMENTS)
    for directory in (root / "docs", root / ".agents/skills"):
        names.update(str(path.relative_to(root)) for path in directory.rglob("*.md"))
    return (*TEXT_DOCUMENTS, *sorted(names.difference(TEXT_DOCUMENTS)))


def _document_links(text: str) -> list[str]:
    inline = re.findall(r"\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)]+)?\s*\)", text)
    references = re.findall(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", text)
    return [angled or plain for angled, plain in (*inline, *references)]


def _local_link_path(link: str, document: Path, root: Path, *, check_external_links: bool = True) -> Path | None:
    target = local_document_link_target(link)
    if target is None:
        return None
    candidate = Path(normpath(document.parent / target))
    document_name = document.relative_to(root).as_posix()
    if not check_external_links and not candidate.is_relative_to(root):
        allowed_external = document_name in {
            MODULE_DOCUMENTS["A4"],
            MODULE_DOCUMENTS["A5"],
        } and _codex_skill_entrypoint(candidate)
        allowed_external = allowed_external or (
            document_name == ".agents/skills/manage-fst-vpn/SKILL.md" and candidate == FST_ACCESS_DOCUMENT
        )
        allowed_external = allowed_external or (
            document_name in {MODULE_DOCUMENTS["M1"], MODULE_DOCUMENTS["M2"]} and candidate in ROLE_JOURNAL_ENTRIES
        )
        if not allowed_external:
            raise ValueError("document_link_invalid")
        return candidate
    resolved = candidate.resolve()
    allowed = resolved.is_relative_to(root)
    if document_name in {MODULE_DOCUMENTS["A4"], MODULE_DOCUMENTS["A5"]}:
        allowed = allowed or (
            _codex_skill_entrypoint(candidate) and _codex_skill_entrypoint(resolved, resolve_roots=True)
        )
    if document_name == ".agents/skills/manage-fst-vpn/SKILL.md":
        allowed = allowed or resolved == FST_ACCESS_DOCUMENT.resolve()
    if document_name in {MODULE_DOCUMENTS["M1"], MODULE_DOCUMENTS["M2"]}:
        allowed = allowed or (
            candidate in ROLE_JOURNAL_ENTRIES and resolved.is_relative_to(ROLE_JOURNAL_ROOT.resolve())
        )
    if not allowed or not resolved.is_file():
        raise ValueError("document_link_invalid")
    return resolved


def _codex_skill_entrypoint(path: Path, *, resolve_roots: bool = False) -> bool:
    skills, cache = (base.resolve() if resolve_roots else base for base in EXTERNAL_CODEX_ROOTS)
    if path.is_relative_to(skills):
        parts = path.relative_to(skills).parts
        return len(parts) == 3 and parts[0] == ".system" and parts[1] != "review-agent" and parts[-1] == "SKILL.md"
    if path.is_relative_to(cache):
        parts = path.relative_to(cache).parts
        return (
            len(parts) == 6
            and parts[0] in {"openai-bundled", "openai-curated-remote", "openai-curated"}
            and parts[1] in EXTERNAL_SKILL_PACKAGES
            and parts[3] == "skills"
            and parts[-1] == "SKILL.md"
        )
    return False


def audit_documentation(root: Path = PROJECT_ROOT, *, check_external_links: bool = True) -> dict[str, Any]:
    """Validate source documents without opening or creating a Manager database."""
    root = root.resolve()
    warnings: list[str] = []
    actual = {"AGENTS.md"}
    actual.update(str(p.relative_to(root)) for p in (root / "docs").rglob("*.md"))
    actual.update(str(p.relative_to(root)) for p in (root / ".agents/skills").rglob("*.md"))
    references = {str(p.relative_to(root)) for p in (root / "docs/agent/references").rglob("*.md")}
    for skill in SKILL_DOCUMENTS:
        directory = (root / skill).parent
        references.update(str(p.relative_to(root)) for p in directory.rglob("*.md") if p.name != "SKILL.md")
    if actual != set(TEXT_DOCUMENTS) | set(REFERENCE_DOCUMENTS) | references:
        warnings.append("instruction_inventory_mismatch")
    size = 0
    detail_size = 0
    linked_paths: dict[str, set[Path]] = {}
    paths = instruction_paths(root)
    for name in paths:
        path = root / name
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("outside_project")
            text = path.read_text(encoding="utf-8")
            if name in TEXT_DOCUMENTS:
                size += len(text.encode())
            else:
                detail_size += len(text.encode())
            if not text.strip():
                warnings.append(f"empty_document:{name}")
            if (
                name in MODULE_DOCUMENTS.values()
                and name != "AGENTS.md"
                and not re.search(r"(?m)^#\s+" + re.escape(path.stem) + r"\b", text)
            ):
                warnings.append(f"module_heading_invalid:{name}")
            if name.endswith("SKILL.md"):
                header = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
                fields = header.group(1) if header else ""
                if not re.search(r"(?m)^name: " + re.escape(path.parent.name) + r"$", fields):
                    warnings.append(f"skill_name_invalid:{name}")
                if not re.search(r"(?m)^description: .+", fields):
                    warnings.append(f"skill_description_missing:{name}")
            linked_paths[name] = set()
            for link in _document_links(text):
                try:
                    resolved = _local_link_path(link, path, root, check_external_links=check_external_links)
                except (OSError, RuntimeError, ValueError):
                    warnings.append(f"document_link_invalid:{name}")
                else:
                    if resolved is not None:
                        linked_paths[name].add(resolved)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            warnings.append(f"document_unreadable:{name}")
    required_links = [("AGENTS.md", MODULE_DOCUMENTS["A1"])]
    required_links.extend(
        (MODULE_DOCUMENTS["A1"], MODULE_DOCUMENTS[module_id])
        for module_id, parent in MODULE_PARENTS.items()
        if parent is None and module_id != "A1"
    )
    required_links.extend(
        (MODULE_DOCUMENTS[parent], MODULE_DOCUMENTS[child])
        for child, parent in MODULE_PARENTS.items()
        if parent is not None
    )
    for source, target in required_links:
        try:
            linked = (root / target).resolve() in linked_paths.get(source, set())
        except (OSError, RuntimeError):
            linked = False
        if not linked:
            warnings.append(f"module_link_missing:{source}:{target}")
    if size > INSTRUCTION_BUDGET_BYTES:
        warnings.append("instruction_budget_exceeded")
    return {
        "ok": not warnings,
        "documents": len(TEXT_DOCUMENTS),
        "reference_documents": len(paths) - len(TEXT_DOCUMENTS),
        "bytes": size,
        "detail_bytes": detail_size,
        "modules": len(MODULE_DOCUMENTS),
        "skills": len(SKILL_DOCUMENTS),
        "external_links_checked": check_external_links,
        "warnings": sorted(set(warnings)),
    }


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


def diagnose(*, integrations: bool = False, full: bool = False, check_external_links: bool = True) -> dict[str, Any]:
    from mcp.server.fastmcp import FastMCP

    from .mcp_contract import validate_manager_mcp_surface
    from .mcp_tools import register_manager_tools

    server = FastMCP("AutoStop-local-check")
    register_manager_tools(server)
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    documentation = audit_documentation() if check_external_links else audit_documentation(check_external_links=False)
    checks = {"documentation": documentation, "mcp": validate_manager_mcp_surface(schemas)}
    if integrations:
        from .integration_audit import build_integration_audit

        checks["integrations"] = build_integration_audit(full=full)
        checks["watchdog"] = watchdog_status()
    elif full:
        checks["scope"] = {"ok": False, "warnings": ["full_requires_integrations"]}
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "checks": checks,
        "external_links_checked": check_external_links,
        "warnings": [name for name, check in checks.items() if not check["ok"]],
    }
