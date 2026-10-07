"""Read the canonical instruction graph without importing runtime or business state."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from os.path import normpath
from pathlib import Path
import re

from .document_links import local_document_link_target

BLOCKED_PARTS = frozenset({"archive", "archives", "archived", "draft", "drafts", "history", "reports"})
AUTOMOTIVE_CATALOG = "docs/agent/automotive_tools.json"
MAX_ISSUES = 64


@dataclass(frozen=True)
class InventoryIssue:
    code: str
    source: str
    target: str


@dataclass(frozen=True)
class InstructionInventory:
    paths: tuple[Path, ...]
    issues: tuple[InventoryIssue, ...]


def retired_instruction(path: Path, root: Path) -> bool:
    """Check lexical and resolved paths so an alias cannot reactivate an archive."""
    parts = path.relative_to(root).parts
    return bool(set(parts) & BLOCKED_PARTS) or path.name.endswith("-draft.md")


def collect_instruction_inventory(
    root: Path, *, required_paths: Iterable[str] = (), follow_links: bool = True
) -> InstructionInventory:
    """Collect seeds, visible MD links and declared automotive cards.

    Generated catalogs and external files are leaves, not recursive sources.
    ``follow_links=False`` is only for the stdlib-only Telegram import boundary;
    documentation audits and catalog generation always use the complete graph.
    """
    root = root.resolve()
    issues: list[InventoryIssue] = []

    def label(path: Path) -> str:
        return (path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path))[:512]

    def issue(code: str, source: str, path: Path) -> None:
        item = InventoryIssue(code, source[:512], label(path))
        if item not in issues:
            if len(issues) < MAX_ISSUES:
                issues.append(item)
            elif issues[-1].code != "instruction_inventory_limit":
                issues[-1] = InventoryIssue("instruction_inventory_limit", "AGENTS.md", "AGENTS.md")

    seeds = {root / "AGENTS.md", *(root / name for name in required_paths)}
    seeds.update(p for p in (root / "docs/agent/modules").glob("*.md") if re.fullmatch(r"[A-Z]\d+", p.stem))
    seeds.update((root / ".agents/skills").glob("*/SKILL.md"))
    catalog = root / AUTOMOTIVE_CATALOG
    if catalog.exists() or catalog.is_symlink():
        seeds.add(catalog)
    pending = [(path, label(path)) for path in sorted(seeds)]
    found: set[Path] = set()
    visited: set[Path] = set()
    while pending:
        candidate, source = pending.pop()
        candidate = Path(normpath(candidate))
        if candidate in visited:
            continue
        visited.add(candidate)
        if not candidate.is_relative_to(root):
            issue("outside", source, candidate)
            continue
        if retired_instruction(candidate, root):
            issue("retired", source, candidate)
            continue
        try:
            path = candidate.resolve()
            if not path.is_relative_to(root):
                issue("outside", source, candidate)
                continue
            if retired_instruction(path, root):
                issue("retired", source, candidate)
                continue
            if not path.is_file():
                issue("missing", source, candidate)
                continue
            if candidate == catalog:
                refs = _automotive_references(path)
                pending.extend((root / ref, AUTOMOTIVE_CATALOG) for ref in refs)
            if path in found:
                continue
            found.add(path)
            if path.suffix != ".md" or path.name in {"A4.md", "A5.md"} or not follow_links:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, RuntimeError, UnicodeError, ValueError):
            issue("unreadable", source, candidate)
            continue
        pending.extend(_linked_instructions(path, text, root, issue))
    return InstructionInventory(tuple(sorted(found)), tuple(issues))


def _linked_instructions(
    path: Path, text: str, root: Path, issue: Callable[[str, str, Path], None]
) -> list[tuple[Path, str]]:
    # Keep markdown-it out of Telegram's minimal, site-package-free import lane.
    from .markdown_links import visible_markdown_links

    source = path.relative_to(root).as_posix()
    targets: list[tuple[Path, str]] = []
    for link in visible_markdown_links(text):
        try:
            destination = local_document_link_target(link)
        except ValueError as exc:
            code = "document_link_line_invalid" if str(exc) == "document_link_line_invalid" else "invalid_link"
            issue(code, source, path)
            continue
        if destination is None:
            continue
        target = Path(normpath(path.parent / destination))
        # Absolute external entrypoints are validated by each consumer's
        # allowlist, but their content is never part of the project graph.
        if Path(destination).is_absolute() and not target.is_relative_to(root):
            continue
        if target.suffix in {".md", ".json"}:
            targets.append((target, source))
    return targets


def _automotive_references(catalog: Path) -> list[str]:
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
        raise ValueError("instruction_catalog_invalid")
    refs: list[str] = []
    for tool in payload["tools"]:
        ref = tool.get("instruction_ref") if isinstance(tool, dict) else None
        if not isinstance(ref, str) or not ref or Path(ref).is_absolute() or Path(ref).suffix != ".md":
            raise ValueError("instruction_catalog_invalid")
        refs.append(ref)
    return refs


def require_instruction_inventory(inventory: InstructionInventory) -> list[Path]:
    """Keep the catalog generator's strict failure behavior and fixed messages."""
    if inventory.issues:
        item = inventory.issues[0]
        if item.code == "retired":
            raise ValueError("Retired instruction is still linked: " + item.target)
        if item.code in {"missing", "outside"}:
            raise ValueError("Missing or outside-project instruction: " + item.target)
        if item.code == "document_link_line_invalid":
            raise ValueError(item.code)
        raise ValueError("Invalid or unreadable instruction: " + item.target)
    return list(inventory.paths)
