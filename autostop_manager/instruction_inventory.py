"""Read the canonical instruction graph without importing runtime or business state."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat

from .document_links import LocalDocumentLink, parse_local_document_link, validate_document_reference

BLOCKED_PARTS = frozenset({"archive", "archives", "archived", "draft", "drafts", "history", "reports"})
# This migration report documents an earlier release, not a client process.
# It remains in Git as history and must never become an operational instruction.
HISTORICAL_DOCUMENTS = frozenset({"docs/agent/references/client-instruction-audit.md"})
AUTOMOTIVE_CATALOG = "docs/agent/automotive_tools.json"
MAX_ISSUES = 64
MAX_INSTRUCTION_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InventoryIssue:
    code: str
    source: str
    target: str


@dataclass(frozen=True)
class InstructionInventory:
    paths: tuple[Path, ...]
    issues: tuple[InventoryIssue, ...]
    # One operation shares the graph's exact bytes with audit/hash consumers.
    contents: dict[Path, bytes] = field(default_factory=dict)
    unavailable: dict[Path, str] = field(default_factory=dict)


def read_instruction_bytes(path: Path) -> bytes:
    """Bound reads before allocation and reject special files without blocking."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("instruction_not_regular")
        if info.st_size > MAX_INSTRUCTION_BYTES:
            raise ValueError("instruction_too_large")
        content = stream.read(MAX_INSTRUCTION_BYTES + 1)
    if len(content) > MAX_INSTRUCTION_BYTES:
        raise ValueError("instruction_too_large")
    return content


def retired_instruction(path: Path, root: Path) -> bool:
    """Check lexical and resolved paths so an alias cannot reactivate an archive."""
    parts = path.relative_to(root).parts
    return (
        bool(set(parts) & BLOCKED_PARTS)
        or path.name.endswith("-draft.md")
        or path.relative_to(root).as_posix() in HISTORICAL_DOCUMENTS
    )


def active_instruction_candidates(root: Path) -> set[Path]:
    """Inventory active document files independently of navigation coverage."""
    root = root.resolve()
    candidates = {root / "AGENTS.md"}
    for directory in (root / "docs/agent", root / ".agents/skills"):
        candidates.update(
            path
            for path in directory.rglob("*")
            if path.suffix in {".md", ".json"} and not retired_instruction(path, root)
        )
    return candidates


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
    contents: dict[Path, bytes] = {}
    unavailable: dict[Path, str] = {}
    references: list[tuple[Path, str, LocalDocumentLink]] = []
    while pending:
        candidate, source = pending.pop()
        if candidate in visited:
            continue
        visited.add(candidate)
        if not candidate.is_relative_to(root):
            issue("outside", source, candidate)
            continue
        if retired_instruction(candidate, root):
            issue("retired", source, candidate)
            continue
        path = candidate
        try:
            # Resolve the original path: collapsing '..' first changes symlink
            # traversal and can turn an outside target into an approved file.
            path = candidate.resolve()
            if not path.is_relative_to(root):
                issue("outside", source, candidate)
                continue
            if retired_instruction(path, root):
                issue("retired", source, candidate)
                continue
            if not path.is_file():
                unavailable[path] = "unavailable"
                issue("missing", source, candidate)
                continue
            if path not in contents:
                contents[path] = read_instruction_bytes(path)
            # Leaves still have a text contract. Skipping their outgoing links
            # must not turn broken encoding into healthy instruction readiness.
            text = contents[path].decode("utf-8")
            if candidate == catalog:
                refs = _automotive_references(text)
                pending.extend((root / ref, AUTOMOTIVE_CATALOG) for ref in refs)
            if path in found:
                continue
            found.add(path)
            if path.suffix != ".md" or path.name in {"A4.md", "A5.md"} or not follow_links:
                continue
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            too_large = isinstance(exc, ValueError) and str(exc) == "instruction_too_large"
            unavailable[path] = "too_large" if too_large else "unavailable"
            issue("too_large" if too_large else "unreadable", source, candidate)
            continue
        pending.extend(_linked_instructions(path, text, root, issue, references))
    _validate_references(root, references, contents, unavailable, issue)
    return InstructionInventory(tuple(sorted(found)), tuple(issues), contents, unavailable)


def _validate_references(
    root: Path,
    references: list[tuple[Path, str, LocalDocumentLink]],
    contents: dict[Path, bytes],
    unavailable: dict[Path, str],
    issue: Callable[[str, str, Path], None],
) -> None:
    for candidate, source, reference in references:
        try:
            path = candidate.resolve()
            if not candidate.is_relative_to(root) or not path.is_relative_to(root):
                issue("outside", source, candidate)
                continue
            if retired_instruction(candidate, root) or retired_instruction(path, root):
                issue("retired", source, candidate)
                continue
            if path in unavailable:
                continue
            if not path.is_file():
                issue("missing", source, candidate)
                continue
            if path not in contents:
                contents[path] = read_instruction_bytes(path)
            validate_document_reference(reference, path, contents[path].decode("utf-8"))
        except ValueError as exc:
            issue(str(exc), source, candidate)
        except (OSError, RuntimeError, UnicodeError):
            issue("unreadable", source, candidate)


def _linked_instructions(
    path: Path,
    text: str,
    root: Path,
    issue: Callable[[str, str, Path], None],
    references: list[tuple[Path, str, LocalDocumentLink]],
) -> list[tuple[Path, str]]:
    # Keep markdown-it out of Telegram's minimal, site-package-free import lane.
    from .markdown_links import visible_markdown_links

    source = path.relative_to(root).as_posix()
    targets: list[tuple[Path, str]] = []
    for link in visible_markdown_links(text):
        try:
            reference = parse_local_document_link(link)
        except ValueError as exc:
            code = "document_link_line_invalid" if str(exc) == "document_link_line_invalid" else "invalid_link"
            issue(code, source, path)
            continue
        if reference is None:
            continue
        destination = reference.path
        target = path.parent / destination if destination else path
        # Absolute external entrypoints are validated by each consumer's
        # allowlist, but their content is never part of the project graph.
        if Path(destination).is_absolute() and not target.is_relative_to(root):
            continue
        if reference.line is not None or reference.fragment:
            references.append((target, source, reference))
        if target.suffix in {".md", ".json"}:
            targets.append((target, source))
    return targets


def _automotive_references(text: str) -> list[str]:
    payload = json.loads(text)
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
        if item.code in {
            "document_link_line_invalid",
            "document_link_line_out_of_range",
            "document_link_anchor_missing",
        }:
            raise ValueError(item.code)
        raise ValueError("Invalid or unreadable instruction: " + item.target)
    return list(inventory.paths)
